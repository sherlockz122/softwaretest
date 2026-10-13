"""Artificial fixtures validate provenance boundaries, not real label accuracy."""

import csv
import hashlib
import io
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import source_evidence as module
from packages.mining.public_dataset import DATE_FIELDS, PublicDataError
from packages.mining.source_catalog import PROJECTS
from packages.mining.source_evidence import LINK_FIELDS, audit_sources, content_hash
from scripts import audit_source_evidence as cli
from scripts import fetch_source_evidence as fetch
from scripts.fetch_public_dataset import ROOT

ACQUIRED = "2026-01-01T00:00:00+00:00"


def write(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fixture(tmp_path, change=None):
    dates = [
        dict(
            zip(
                DATE_FIELDS,
                (
                    f"{i:040x}",
                    "apache/activemq",
                    "1970-01-01 00:01:40" if i == 1 else "",
                    "1970-01-01 00:03:20" if i == 1 else "",
                    str(i == 1),
                    "False",
                    "100",
                    "2003",
                ),
                strict=True,
            )
        )
        for i in (1, 2, 3)
    ]
    links = [
        dict(zip(LINK_FIELDS, (f"{f:040x}", str(t), f"{b:040x}", "100", "AMQ"), strict=True))
        for f, t, b in ((20, 150, 1), (21, 200, 1), (22, 210, 2))
    ]
    if change:
        change(dates, links)
    date_path, link_path = tmp_path / "date.csv", tmp_path / "links.csv"
    write(date_path, DATE_FIELDS, dates)
    write(link_path, LINK_FIELDS, links)
    return date_path, {"AMQ": link_path}


def test_real_boundaries_determinism_and_independent_copy(tmp_path):
    paths = fixture(tmp_path)
    audit = audit_sources(*paths, acquired_at=ACQUIRED)
    result = audit.as_dict()
    counts = result["projects"]["apache/activemq"]["counts"]
    assert counts["buggy_raw_candidate"] == counts["selected_fix_matches_raw"] == 1
    assert counts["selected_fix_not_earliest_raw"] == counts["clean_raw_candidate"] == 1
    assert counts["clean_no_raw_candidate"] == 1
    assert result == audit_sources(*paths, acquired_at=ACQUIRED).as_dict()
    assert result["content_hash"] == content_hash(
        {k: v for k, v in result.items() if k != "content_hash"}
    )
    assert all(p["historical_available_at"] is None for p in result["review_packet"])
    assert not result["product_training_ready"] and not result["historical_replay_ready"]
    assert not result["negative_coverage_complete"] and result["noise_rate"] is None
    assert len(result["training_deficits"]) == 6
    result["review_packet"][0]["review_state"] = "gold"
    assert audit.as_dict()["review_packet"][0]["review_state"] == "pending"


def test_absent_source_and_missing_positive_not_assumed_clean(tmp_path):
    paths = fixture(tmp_path, lambda dates, _: dates[0].update(project="apache/hadoop"))
    report = audit_sources(*paths, acquired_at=ACQUIRED).as_dict()
    assert report["projects"]["apache/hadoop"]["counts"]["buggy_source_missing"] == 1
    assert PROJECTS["HDFS"] != PROJECTS["MAPREDUCE"]


def test_duplicate_and_inverted_raw_links_remain_diagnostics(tmp_path):
    def mutate(_, links):
        links.extend([dict(links[0]), dict(links[0], fix_date="50")])

    report = audit_sources(*fixture(tmp_path, mutate), acquired_at=ACQUIRED).as_dict()
    counts = report["raw_projects"]["apache/activemq"]
    assert counts["duplicate_rows"] == counts["non_forward_clock_rows"] == 1
    assert counts["pairs_with_conflicting_clocks"] == 1 and counts["unique_pairs"] == 3


def test_review_quota_order_and_link_truncation(tmp_path):
    def mutate(dates, links):
        for i in range(4, 24):
            dates.append(dict(dates[-1], commit_id=f"{i:040x}"))
        for i in range(100, 115):
            links.append(dict(links[0], fix_hash=f"{i:040x}", fix_date=str(i + 100)))

    paths = fixture(tmp_path, mutate)
    report = audit_sources(*paths, acquired_at=ACQUIRED).as_dict()
    packet = report["review_packet"]
    assert len(packet) == 4
    buggy = next(p for p in packet if p["published_label"] == "buggy")
    assert buggy["raw_link_count"] == 17 and len(buggy["display_links"]) == 8
    assert buggy["display_links_truncated"]
    rows = list(csv.DictReader(paths[0].open()))
    write(paths[0], DATE_FIELDS, reversed(rows))
    reversed_result = audit_sources(*paths, acquired_at=ACQUIRED).as_dict()
    assert reversed_result["review_packet"] == packet
    assert reversed_result["projects"] == report["projects"]
    assert reversed_result["content_hash"] != report["content_hash"]  # bytes are bound as well


@pytest.mark.parametrize("bad", [None, "", "2026-01-01", "2021-01-01T00:00:00Z"])
def test_acquisition_clock_rejected(tmp_path, bad):
    with pytest.raises(PublicDataError, match="ACQUISITION"):
        audit_sources(*fixture(tmp_path), acquired_at=bad)


def test_future_acquisition_rejected(tmp_path):
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    with pytest.raises(PublicDataError, match="ACQUISITION"):
        audit_sources(*fixture(tmp_path), acquired_at=future)


@pytest.mark.parametrize(
    "options,code",
    [
        ({"max_bytes": True}, "LIMIT_INVALID"),
        ({"max_rows": 0}, "LIMIT_INVALID"),
        ({"seconds": float("nan")}, "LIMIT_INVALID"),
        ({"seconds": 121}, "LIMIT_INVALID"),
        ({"max_bytes": 1}, "BYTE_LIMIT"),
        ({"max_rows": 5}, "ROW_LIMIT"),
    ],
)
def test_resource_guards(tmp_path, options, code):
    with pytest.raises(PublicDataError, match=code):
        audit_sources(*fixture(tmp_path), acquired_at=ACQUIRED, **options)


def test_supervision_cancel_deadline_and_input_mutation(tmp_path, monkeypatch):
    paths = fixture(tmp_path)

    def cancel():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        audit_sources(*paths, acquired_at=ACQUIRED, tick=cancel)
    clock = iter([0, 121])
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: next(clock))
        with pytest.raises(PublicDataError, match="DEADLINE"):
            audit_sources(*paths, acquired_at=ACQUIRED)
    original = module.fingerprint
    calls = 0

    def changing(path, **options):
        nonlocal calls
        calls += 1
        if calls == 3:
            paths[0].write_bytes(paths[0].read_bytes() + b"\n")
        return original(path, **options)

    monkeypatch.setattr(module, "fingerprint", changing)
    with pytest.raises(PublicDataError, match="INPUT_CHANGED"):
        audit_sources(*paths, acquired_at=ACQUIRED)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("project", "HIVE", "LINK_ID"),
        ("bug_hash", "private", "LINK_ID"),
        ("fix_date", "-1", "EVENT"),
        ("bug_date", "NaN", "EVENT"),
    ],
)
def test_bad_raw_schema_identifiers_and_clocks(tmp_path, field, value, code):
    paths = fixture(tmp_path, lambda _, links: links[0].update({field: value}))
    with pytest.raises(PublicDataError, match=code):
        audit_sources(*paths, acquired_at=ACQUIRED)


def test_duplicate_published_and_malformed_row_rejected(tmp_path):
    paths = fixture(tmp_path, lambda dates, _: dates.append(dict(dates[0])))
    with pytest.raises(PublicDataError, match="DUPLICATE_KEY"):
        audit_sources(*paths, acquired_at=ACQUIRED)
    paths = fixture(tmp_path)
    with paths[1]["AMQ"].open("a") as stream:
        stream.write("a,b,c\n")
    with pytest.raises(PublicDataError, match="ROW_INVALID"):
        audit_sources(*paths, acquired_at=ACQUIRED)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    raw = b"source-only-never-execute\n"
    blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    monkeypatch.setattr(fetch, "SOURCES", (("data/test.csv", len(raw), blob),))

    class Opener:
        def open(self, request, **_):
            assert fetch.REVISION in request.full_url
            return io.BytesIO(raw)

    monkeypatch.setattr(fetch.urllib.request, "build_opener", lambda *_: Opener())
    root = ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="source-test-", dir=root) as folder:
        yield Path(folder)


def test_acquisition_reuse_integrity_and_manifest_time(bundle, monkeypatch):
    first = fetch.acquire(bundle)

    def no_network(*_):
        raise AssertionError("valid reuse must not download")

    monkeypatch.setattr(fetch.urllib.request, "build_opener", no_network)
    assert first == fetch.acquire(bundle)
    assert first["upstream_code_executed"] is False
    assert first["raw_source_license_verified"] is False
    source = bundle / "test.csv"
    source.write_bytes(b"x" * source.stat().st_size)
    with pytest.raises(ValueError, match="integrity"):
        fetch.verify_bundle(bundle)


def test_manifest_mutation_rejected(bundle):
    fetch.acquire(bundle)
    manifest = bundle / "acquisition.json"
    value = json.loads(manifest.read_text())
    value["acquired_at"] = ACQUIRED
    manifest.write_text(json.dumps(value))
    with pytest.raises(PublicDataError, match="MANIFEST_INVALID"):
        fetch.verify_bundle(bundle)


def test_atomic_no_clobber_and_link_failure_cleanup(bundle, monkeypatch):
    output = bundle / "report.json"
    fetch.publish_json(output, {"safe": True})
    original = output.read_bytes()
    with pytest.raises(PublicDataError, match="OUTPUT_EXISTS"):
        fetch.publish_json(output, {"safe": False})
    assert output.read_bytes() == original

    def failure(*_):
        raise OSError("cannot link")

    monkeypatch.setattr(fetch.os, "link", failure)
    with pytest.raises(OSError, match="cannot link"):
        fetch.publish_json(bundle / "failed.json", {"safe": True})
    assert not (bundle / "failed.json").exists()
    assert not list(bundle.glob("*.part"))


def test_paths_outside_runtime_rejected(tmp_path):
    with pytest.raises(ValueError, match="runtime"):
        fetch.publish_json(ROOT / "docs/unsafe-source-report.json", {})


def test_acquisition_bad_download_never_publishes(bundle, monkeypatch):
    class Opener:
        def open(self, *_args, **_options):
            return io.BytesIO(b"wrong")

    monkeypatch.setattr(fetch.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(ValueError, match="integrity"):
        fetch.acquire(bundle)
    assert not (bundle / "acquisition.json").exists()
    assert not (bundle / "test.csv").exists()
    assert not list(bundle.glob("*.part"))


def test_same_git_repo_cohorts_are_not_merged(tmp_path):
    paths = fixture(tmp_path)
    dates = list(csv.DictReader(paths[0].open()))
    dates[0]["project"] = "apache/hadoop-hdfs"
    dates[1]["project"] = "apache/hadoop-mapreduce"
    write(paths[0], DATE_FIELDS, dates)
    linked = list(csv.DictReader(paths[1]["AMQ"].open()))
    mapping = {}
    for code in ("HDFS", "MAPREDUCE"):
        path = tmp_path / f"{code}.csv"
        write(path, LINK_FIELDS, [dict(row, project=code) for row in linked])
        mapping[code] = path
    result = audit_sources(paths[0], mapping, acquired_at=ACQUIRED).as_dict()
    assert result["projects"][PROJECTS["HDFS"]]["counts"]["buggy"] == 1
    assert result["projects"][PROJECTS["MAPREDUCE"]]["counts"]["clean"] == 1
    assert len(result["raw_projects"]) == 2


def test_schema_and_path_collision(tmp_path):
    dates, links = fixture(tmp_path)
    with pytest.raises(PublicDataError, match="PATH_COLLISION"):
        audit_sources(dates, {"AMQ": dates}, acquired_at=ACQUIRED)
    links["AMQ"].write_text("unexpected,header\n")
    with pytest.raises(PublicDataError, match="SCHEMA_INVALID"):
        audit_sources(dates, links, acquired_at=ACQUIRED)


def test_oversize_download_cleanup(bundle, monkeypatch):
    class Opener:
        def open(self, *_args, **_options):
            return io.BytesIO(b"x" * 1024)

    monkeypatch.setattr(fetch.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(PublicDataError, match="DOWNLOAD_LIMIT"):
        fetch.acquire(bundle)
    assert list(bundle.iterdir()) == []


def test_slow_download_deadline_cleanup(bundle, monkeypatch):
    clock = iter((0, 0, 121))
    monkeypatch.setattr(fetch.time, "monotonic", lambda: next(clock))
    with pytest.raises(PublicDataError, match="DOWNLOAD_DEADLINE"):
        fetch.acquire(bundle)
    assert list(bundle.iterdir()) == []


def test_publish_race_preserves_other_writers_complete_file(bundle, monkeypatch):
    output = bundle / "race.json"
    link = fetch.os.link

    def race(part, target):
        target.write_bytes(b"existing-complete-result")
        link(part, target)

    monkeypatch.setattr(fetch.os, "link", race)
    with pytest.raises(FileExistsError):
        fetch.publish_json(output, {"new": True})
    assert output.read_bytes() == b"existing-complete-result"
    assert not list(bundle.glob("*.part"))


def test_cli_publishes_joined_bound_manifest_and_refuses_reuse(bundle, monkeypatch):
    dates, links = fixture(bundle)
    dates.rename(bundle / "apachejit_date.csv")
    links["AMQ"].rename(bundle / "commit_links_AMQ.csv")
    (bundle / "apachejit_total.csv").write_text("prevalidated by public audit fixture")
    dsha = module.fingerprint(bundle / "apachejit_date.csv")
    lsha = module.fingerprint(bundle / "commit_links_AMQ.csv")
    release = {"files": [{"sha256": "total-hash"}, {"sha256": dsha}]}
    raw = {"acquired_at": ACQUIRED, "files": [{"sha256": lsha}]}
    monkeypatch.setattr(cli, "PROJECTS", {"AMQ": PROJECTS["AMQ"]})
    monkeypatch.setattr(cli, "verify_manifest", lambda _: release)
    monkeypatch.setattr(cli, "verify_bundle", lambda _: raw)
    monkeypatch.setattr(
        cli,
        "audit_paths",
        lambda *_: {
            "source_sha256": {"total": "total-hash", "date": dsha},
            "audit_version": "fixture-v1",
        },
    )
    out_root = ROOT / "runtime/acceptance"
    with TemporaryDirectory(prefix="source-cli-test-", dir=out_root) as folder:
        output = Path(folder) / "report.json"
        result = cli.run(bundle, bundle, output)
        assert result == json.loads(output.read_text())
        assert result["published_feature_version"] == "fixture-v1"
        assert result["raw_acquisition"] == raw
        assert not result["product_training_ready"]
        with pytest.raises(PublicDataError, match="OUTPUT_INVALID"):
            cli.run(bundle, bundle, output)
