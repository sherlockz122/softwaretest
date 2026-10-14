"""Synthetic integrity fixtures; they do not establish real label/model accuracy."""

import csv
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import asset_reviews as reviews
from packages.mining import public_assets as assets
from packages.mining.public_dataset import DATE_FIELDS, NUMERIC_FIELDS, TOTAL_FIELDS
from packages.mining.source_evidence import LINK_FIELDS, audit_sources, canonical, content_hash
from scripts import probe_review_links, public_asset, public_review

ACQUIRED = "2022-01-01T00:00:00+00:00"


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@contextmanager
def workspace():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="asset-unit-", dir=root) as folder:
        yield Path(folder)


def source(root, *, projects=("apache/activemq",)):
    directory = root / "published"
    directory.mkdir()
    totals, dates = [], []
    for project in projects:
        for i in range(1, 10):
            sha, event, buggy = f"{i:040x}", 100 * i, i % 2 == 0
            total = dict(
                zip(
                    TOTAL_FIELDS,
                    (
                        sha,
                        project,
                        str(buggy),
                        "False",
                        "2003",
                        str(event),
                        *(str(i) for _ in NUMERIC_FIELDS),
                    ),
                    strict=True,
                )
            )
            date = dict(
                zip(
                    DATE_FIELDS,
                    (
                        sha,
                        project,
                        datetime.fromtimestamp(event, UTC).isoformat() if buggy else "",
                        datetime.fromtimestamp(event + 100, UTC).isoformat() if buggy else "",
                        str(buggy),
                        "False",
                        str(event),
                        "2003",
                    ),
                    strict=True,
                )
            )
            totals.append(total)
            dates.append(date)
    write_csv(directory / "apachejit_total.csv", TOTAL_FIELDS, totals)
    write_csv(directory / "apachejit_date.csv", DATE_FIELDS, dates)
    receipt = {"acquired_at": ACQUIRED, "fixture_not_real_acquisition": True}
    (directory / "acquisition.json").write_bytes(canonical(receipt))
    for name in assets.SOURCE_NAMES - {
        "apachejit_total.csv",
        "apachejit_date.csv",
        "acquisition.json",
    }:
        (directory / name).write_text("synthetic inert material", encoding="utf-8")
    return directory, receipt


def verifier(directory):
    return json.loads((directory / "acquisition.json").read_text(encoding="utf-8"))


def imported(root, projects=("apache/activemq",)):
    published, receipt = source(root, projects=projects)
    value = assets.import_asset(published, root / "store", verify_source=verifier)
    directory = root / "store" / value["manifest"]["asset_id"]
    return published, receipt, directory, value


def review_context(root):
    published, receipt, directory, asset = imported(root)
    link_path = root / "links.csv"
    write_csv(
        link_path,
        LINK_FIELDS,
        [dict(zip(LINK_FIELDS, (f"{20:040x}", "300", f"{2:040x}", "200", "AMQ"), strict=True))],
    )
    report = audit_sources(
        published / "apachejit_date.csv", {"AMQ": link_path}, acquired_at=ACQUIRED
    ).as_dict()
    report["published_acquisition"] = receipt
    report.pop("content_hash")
    report["content_hash"] = content_hash(report)
    report_path = root / "report.json"
    report_path.write_bytes(canonical(report) + b"\n")
    initial = reviews.initialize_reviews(report_path, directory, root / "reviews")
    context = root / "reviews" / initial["context_id"]
    case = next(r for r in initial["records"] if r["packet"]["raw_link_count"])
    return context, initial, case, report_path, directory


def evidence(root, initial, case, **extra):
    p = root / "evidence.json"
    p.write_bytes(
        canonical(
            assets.sealed(
                {
                    "version": "source-filter-notes-v1",
                    "asset_id": initial["binding"]["asset_id"],
                    "report_hash": initial["binding"]["report_hash"],
                    "case_id": case["case_id"],
                    "observed_at": datetime.now(UTC).isoformat(),
                    "note": "fixture only, not independent label evidence",
                    **extra,
                }
            )
        )
        + b"\n"
    )
    return p


def update(context, case, **extra):
    return reviews.append_review(
        context,
        case["case_id"],
        expected_revision=case["revision"],
        expected_hash=case["content_hash"],
        status="unresolved",
        operator="fixture",
        rationale="Incomplete observation",
        **extra,
    )


def test_roundtrip_original_numeric_metadata_and_idempotency():
    with workspace() as root:
        published, receipt, directory, first = imported(root)
        for name in assets.SOURCE_NAMES:
            assert (directory / "source" / name).read_bytes() == (published / name).read_bytes()
        assert assets.load_asset(directory) == first
        again = assets.import_asset(published, root / "store", verify_source=verifier)
        assert again == first and first["manifest"]["identity"]["source_acquisition"] == receipt
        page = assets.page_asset(directory, offset=1, limit=2)
        assert page["total"] == 9 and len(page["items"]) == 2
        row = page["items"][0]
        assert row["numeric12"] == dict.fromkeys(NUMERIC_FIELDS, 2.0)
        assert row["original_fields"]["fix"] == "False" and row["published_fix_metadata"] is False
        assert row["defect_types"] is None and row["label_available_at"] is None
        assert row["languages"] == [] and not row["semantic_available"]
        assert first["quality"]["capabilities"] == assets.CAPABILITIES
        assert assets.catalog(root / "store") == [first]
        assert not list((root / "store").glob(".staging-*"))
        first["quality"]["rows"] = 999
        assert assets.load_asset(directory)["quality"]["rows"] == 9


def test_hadoop_cohorts_remain_separate_canonical_repository_shared():
    with workspace() as root:
        _, _, directory, _ = imported(root, ("apache/hadoop-hdfs", "apache/hadoop-mapreduce"))
        rows = assets.page_asset(directory, limit=20)["items"]
        assert {r["cohort"] for r in rows} == {"apache/hadoop-hdfs", "apache/hadoop-mapreduce"}
        assert {r["canonical_repository"] for r in rows} == {"apache/hadoop"}


@pytest.mark.parametrize(
    "name",
    ["normalized.jsonl", "quality.json", "source/apachejit_total.csv", "source/acquisition.json"],
)
def test_tampered_files_rejected(name):
    with workspace() as root:
        _, _, directory, _ = imported(root)
        with (directory / name).open("ab") as f:
            f.write(b" ")
        with pytest.raises(assets.AssetError, match="ASSET_FILE_DRIFT"):
            assets.load_asset(directory)


@pytest.mark.parametrize("change", ["seal", "identity", "extra", "duplicate", "time"])
def test_manifest_tampering_and_duplicate_keys(change):
    with workspace() as root:
        _, _, directory, value = imported(root)
        manifest = value["manifest"]
        if change == "seal":
            manifest["first_stored_at"] = ACQUIRED
        elif change == "identity":
            manifest["identity"]["adapter_rule_hash"] = "0" * 64
            manifest = assets.sealed({k: v for k, v in manifest.items() if k != "content_hash"})
        elif change == "extra":
            (directory / "unexpected.py").write_text("do not run")
        elif change == "time":
            manifest["first_stored_at"] = "2022-01-01"
            manifest = assets.sealed({k: v for k, v in manifest.items() if k != "content_hash"})
        if change != "extra":
            raw = canonical(manifest) + b"\n"
            if change == "duplicate":
                raw = raw[:-2] + b',"asset_id":"duplicate"}\n'
            (directory / "manifest.json").write_bytes(raw)
        with pytest.raises(assets.AssetError):
            assets.load_asset(directory)


def test_path_boundary_and_symlink(monkeypatch):
    with workspace() as root:
        with pytest.raises(assets.AssetError, match="OUTSIDE_RUNTIME"):
            assets.safe_path(assets.ROOT / "docs" / "x")
        path = root / "link"
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == path or original(p))
        with pytest.raises(assets.AssetError, match="PATH_LINK"):
            assets.safe_path(path / "child")


def test_actual_link_if_supported():
    with workspace() as root:
        _, _, directory, _ = imported(root)
        p = directory / "quality.json"
        target = root / "target.json"
        shutil.copyfile(p, target)
        p.unlink()
        try:
            p.symlink_to(target)
        except OSError:
            # Windows user policy may deny symlink creation; deterministic link guard above remains.
            shutil.copyfile(target, p)
            assert assets.load_asset(directory)["quality"]["rows"] == 9
        else:
            with pytest.raises(assets.AssetError, match="PATH_LINK"):
                assets.load_asset(directory)


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 1001), (True, 2)])
def test_page_limits(offset, limit):
    with workspace() as root:
        _, _, directory, _ = imported(root)
        with pytest.raises(assets.AssetError, match="PAGE_INVALID"):
            assets.page_asset(directory, offset=offset, limit=limit)


def test_resource_deadline_cancel_source_drift_and_cleanup(monkeypatch):
    with workspace() as root:
        published, _ = source(root)
        store = root / "store"
        real = assets.normalized_rows

        def cancelled(*args):
            yield next(real(*args))
            raise assets.AssetError("TEST_CANCELLED")

        monkeypatch.setattr(assets, "normalized_rows", cancelled)
        with pytest.raises(assets.AssetError, match="TEST_CANCELLED"):
            assets.import_asset(published, store, verify_source=verifier)
        assert not list(store.iterdir())

        def drift(*args):
            yield from real(*args)
            with (published / "apachejit_total.csv").open("ab") as f:
                f.write(b" ")

        monkeypatch.setattr(assets, "normalized_rows", drift)
        with pytest.raises(assets.AssetError, match="SOURCE_CHANGED"):
            assets.import_asset(published, store, verify_source=verifier)
        assert not list(store.iterdir())
    with pytest.raises(assets.AssetError, match="LIMIT_INVALID"):
        assets.Budget(seconds=float("nan"))
    b = assets.Budget()
    b.deadline = 0
    with pytest.raises(assets.AssetError, match="DEADLINE"):
        b.check()
    monkeypatch.setattr(assets.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 1})())
    with pytest.raises(assets.AssetError, match="CAPACITY_UNSAFE"):
        assets.Budget().check()


@pytest.mark.parametrize("kind", ["rows", "field", "bytes"])
def test_input_limits_no_publication(kind, monkeypatch):
    with workspace() as root:
        published, _ = source(root)
        if kind == "rows":
            monkeypatch.setattr(assets, "MAX_ROWS", 2)
        elif kind == "bytes":
            monkeypatch.setattr(assets, "MAX_BYTES", 10)
        else:
            rows = list(csv.DictReader((published / "apachejit_total.csv").open(encoding="utf-8")))
            rows[0]["la"] = "0" * 4097
            write_csv(published / "apachejit_total.csv", TOTAL_FIELDS, rows)
        with pytest.raises(assets.AssetError):
            assets.import_asset(published, root / "store", verify_source=verifier)
        assert not list((root / "store").iterdir())


def test_parallel_import_preserves_one_complete_asset():
    with workspace() as root:
        published, _ = source(root)
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(
                pool.map(
                    lambda _: assets.import_asset(
                        published, root / "store", verify_source=verifier
                    ),
                    range(2),
                )
            )
        assert values[0] == values[1]
        assert len(list((root / "store").iterdir())) == 1


def test_review_revision_evidence_chain_and_independence():
    with workspace() as root:
        directory, initial, case, report, asset = review_context(root)
        ev = evidence(root, initial, case)
        row = update(directory, case, evidence_paths=[ev])
        assert row["revision"] == 1 and row["previous_hash"] == case["content_hash"]
        assert row["gold_certified"] is False and row["historical_available_at"] is None
        second = update(directory, row)
        assert second["revision"] == 2
        assert len(reviews.case_history(directory, case["case_id"])["records"]) == 3
        assert reviews.initialize_reviews(report, asset, root / "reviews") == initial
        with pytest.raises(assets.AssetError, match="REVIEW_CONFLICT"):
            update(directory, case)
        row["status"] = "conflict"
        assert (
            reviews.case_history(directory, case["case_id"])["records"][1]["status"] == "unresolved"
        )
        ev.write_bytes(ev.read_bytes() + b" ")
        with pytest.raises(assets.AssetError, match="EVIDENCE_CHANGED"):
            reviews.case_history(directory, case["case_id"])


@pytest.mark.parametrize("kind", ["member", "report", "predecessor", "gap", "evidence", "case"])
def test_review_binding_drift(kind):
    with workspace() as root:
        directory, initial, case, report, asset = review_context(root)
        if kind == "member":
            value = json.loads(report.read_text())
            value["review_packet"][0]["commit_id"] = "f" * 40
            report.write_bytes(
                canonical(assets.sealed({k: v for k, v in value.items() if k != "content_hash"}))
                + b"\n"
            )
            with pytest.raises(assets.AssetError, match="MEMBER_CONFLICT"):
                reviews.initialize_reviews(report, asset, root / "other-reviews")
        elif kind == "report":
            report.write_bytes(report.read_bytes().replace(b'"rows":9', b'"rows":8'))
            with pytest.raises(assets.AssetError, match="SEAL_INVALID"):
                reviews.load_initial(directory)
        elif kind in {"predecessor", "gap"}:
            row = update(directory, case)
            p = directory / "cases" / case["case_id"] / "000001.json"
            if kind == "gap":
                p.rename(p.with_name("000002.json"))
            else:
                row["previous_hash"] = "0" * 64
                p.write_bytes(
                    canonical(assets.sealed({k: v for k, v in row.items() if k != "content_hash"}))
                    + b"\n"
                )
            with pytest.raises(assets.AssetError, match="CHAIN_INVALID"):
                reviews.case_history(directory, case["case_id"])
        elif kind == "evidence":
            ev = evidence(root, initial, case, asset_id="0" * 64)
            with pytest.raises(assets.AssetError, match="EVIDENCE_CONFLICT"):
                update(directory, case, evidence_paths=[ev])
        else:
            with pytest.raises(assets.AssetError, match="CASE_MISSING"):
                reviews.case_history(directory, "0" * 64)


@pytest.mark.parametrize("kind", ["operator", "rationale", "status", "no_evidence", "future"])
def test_invalid_review_declarations(kind):
    row = {
        "operator": "local",
        "rationale": "unknown coverage",
        "status": "unresolved",
        "evidence": [],
        "gold_certified": False,
        "historical_available_at": None,
        "label_change_performed": False,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if kind == "operator":
        row["operator"] = "email@example.com"
    elif kind == "rationale":
        row["rationale"] = "secret\nnewline"
    elif kind == "status":
        row["status"] = "gold"
    elif kind == "no_evidence":
        row["status"] = "filter_supported"
    else:
        row["recorded_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(assets.AssetError):
        reviews.validate_declaration(row)


def test_concurrent_review_one_revision_wins():
    with workspace() as root:
        directory, _, case, _, _ = review_context(root)

        def attempt(_):
            try:
                return update(directory, case)["revision"]
            except assets.AssetError as exc:
                assert str(exc) == "REVIEW_CONFLICT"
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(map(str, pool.map(attempt, range(2)))) == ["1", "conflict"]
        assert len(reviews.case_history(directory, case["case_id"])["records"]) == 2


def test_cli_import_page_review_and_safe_errors(monkeypatch, capsys):
    with workspace() as root:
        published, _ = source(root)
        monkeypatch.setattr(public_asset, "verify_manifest", verifier)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "public_asset",
                "--store",
                str(root / "store"),
                "import",
                "--published",
                str(published),
            ],
        )
        public_asset.main()
        asset_id = json.loads(capsys.readouterr().out)["asset_id"]
        monkeypatch.setattr(
            sys,
            "argv",
            ["public_asset", "--store", str(root / "store"), "page", asset_id, "--limit", "1"],
        )
        public_asset.main()
        assert len(json.loads(capsys.readouterr().out)["items"]) == 1
        monkeypatch.setattr(
            sys, "argv", ["public_review", "show", str(root / "missing"), "not-an-id"]
        )
        with pytest.raises(SystemExit) as exc:
            public_review.main()
        assert exc.value.code == 1 and "REVIEW_ID_INVALID" in capsys.readouterr().err


def test_native_probe_missing_objects_keeps_unknown(monkeypatch):
    with workspace() as root:
        (root / "bare").mkdir()
        directory, initial, case, _, _ = review_context(root)
        receipt = root / "git-acquisition.json"
        receipt.write_bytes(canonical({"repository": "apache/activemq", "acquired_at": ACQUIRED}))

        class MissingReader:
            def __init__(self, *args):
                pass

            def command(self, args, **kwargs):
                if args[0] == "rev-parse":
                    return b"true\n"
                assert args[0] == "cat-file"  # No ancestry query when objects are absent.
                return 128

        monkeypatch.setattr(probe_review_links, "NativeReader", MissingReader)
        out = probe_review_links.probe(
            directory, case["case_id"], root / "bare", receipt, root / "native.json"
        )
        assert out["shallow"] and out["results"][0]["ancestry"] == "unknown_missing_objects"
        assert out["historical_available_at"] is None and out["label_certified"] is False
        ev = root / "native.json"
        row = update(directory, case, evidence_paths=[ev])
        assert row["status"] == "unresolved"


def test_native_probe_real_git_clock_and_strict_ancestry():
    with workspace() as root:
        directory, _, case, _, _ = review_context(root)
        repo = root / "git"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

        def git(*args):
            return (
                subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        *args,
                    ]
                )
                .strip()
                .decode()
            )

        (repo / "file.py").write_text("x=1\n")
        git("add", "file.py")
        git("commit", "-m", "initial")
        bug = git("rev-parse", "HEAD")
        (repo / "file.py").write_text("x=2\n")
        git("add", "file.py")
        git("commit", "-m", "fix")
        fix = git("rev-parse", "HEAD")
        # Isolate real Git command behavior from packet SHA selection, which binding tests cover.
        history = reviews.case_history(directory, case["case_id"])
        history["records"][0]["packet"]["commit_id"] = bug
        history["records"][0]["packet"]["display_links"] = [
            {
                "fix_hash": fix,
                "fix_epoch": int(git("show", "-s", "--format=%ct", fix)),
                "bug_epoch": int(git("show", "-s", "--format=%ct", bug)),
            }
        ]
        receipt = root / "git-receipt.json"
        receipt.write_bytes(canonical({"repository": "apache/activemq", "acquired_at": ACQUIRED}))
        original = probe_review_links.case_history
        probe_review_links.case_history = lambda *a, **k: history
        try:
            result = probe_review_links.probe(
                directory, case["case_id"], repo / ".git", receipt, root / "native.json"
            )
        finally:
            probe_review_links.case_history = original
        assert result["results"][0]["ancestry"] == "strict_ancestor"
        assert all(result["results"][0]["raw_clock_matches"].values())
        assert not result["label_certified"]
