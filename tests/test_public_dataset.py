"""Engineered CSV fixtures exercise contracts, not real model performance."""

import csv
import hashlib
import json

import pytest

from packages.mining.public_dataset import (
    DATE_FIELDS,
    TOTAL_FIELDS,
    PublicDataError,
    audit_paths,
    partition,
)
from scripts.fetch_public_dataset import checksums, validate_url, verify_metadata


def sources(tmp_path, change=None, count=30):
    total, dates = [], []
    for i in range(count):
        event = 946684800 + i * 86400
        values = {field: "1" for field in TOTAL_FIELDS}
        values.update(
            commit_id=f"{i + 1:040x}",
            project="apache/example",
            buggy=str(i % 2 == 0),
            fix="False",
            year="2000",
            author_date=str(event),
        )
        total.append(values)
        dates.append(
            {
                "commit_id": values["commit_id"],
                "project": values["project"],
                "bug_date": "",
                "fix_date": "2000-06-01 00:00:00" if i % 2 == 0 else "",
                "buggy": values["buggy"],
                "fix": "False",
                "date": str(event),
                "year": "2000",
            }
        )
    if change:
        change(total, dates)
    paths = tmp_path / "total.csv", tmp_path / "dates.csv"
    for path, schema, records in zip(
        paths, (TOTAL_FIELDS, DATE_FIELDS), (total, dates), strict=True
    ):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=schema)
            writer.writeheader()
            writer.writerows(records)
    return paths


def test_feasibility_is_not_product_or_historical_readiness(tmp_path):
    paths = sources(tmp_path)
    result = audit_paths(*paths)
    assert result["rows"] == 30 and result["labels"] == {"buggy": 15, "clean": 15}
    assert result["published_benchmark_structurally_ready"]
    assert result["structurally_eligible_projects"] == ["apache/example"]
    assert not result["product_training_ready"] and not result["historical_replay_ready"]
    assert result["date_quality"]["fix_dates_without_timezone"] == 15
    assert "LT" not in result["features"]
    assert not result["preprocessing_fitted"] and result["models_trained"] == 0
    assert result == audit_paths(*paths)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1", "", "unavailable"])
def test_invalid_numeric_values_never_imputed(tmp_path, value):
    paths = sources(tmp_path, lambda rows, _: rows[0].update(la=value))
    with pytest.raises(PublicDataError, match="PUBLIC_NUMERIC_INVALID"):
        audit_paths(*paths)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("buggy", "0", "BOOLEAN"),
        ("author_date", "1.5", "EVENT"),
        ("commit_id", "abcdef", "KEY"),
        ("project", "../secret", "KEY"),
        ("year", "oops", "YEAR"),
    ],
)
def test_invalid_identifiers_and_metadata(tmp_path, field, value, code):
    paths = sources(tmp_path, lambda rows, _: rows[0].update({field: value}))
    with pytest.raises(PublicDataError, match="PUBLIC_" + code + "_INVALID"):
        audit_paths(*paths)


@pytest.mark.parametrize("side", [0, 1])
def test_duplicate_source_keys_rejected(tmp_path, side):
    def mutate(total, dates):
        rows = (total, dates)[side]
        rows.append(dict(rows[0]))

    with pytest.raises(PublicDataError, match="PUBLIC_DUPLICATE_KEY"):
        audit_paths(*sources(tmp_path, mutate))


@pytest.mark.parametrize("field", ["buggy", "fix", "year"])
def test_mismatched_label_or_metadata_rejected(tmp_path, field):
    def mutate(_, dates):
        dates[0][field] = "2001" if field == "year" else str(dates[0][field] != "True")

    with pytest.raises(PublicDataError, match="PUBLIC_JOIN_CONFLICT"):
        audit_paths(*sources(tmp_path, mutate))


def test_time_disagreements_and_missing_dates_are_diagnostics(tmp_path):
    def mutate(_, dates):
        dates[0].update(date="946684900", fix_date="")
        dates[2].update(fix_date="1999-01-01 00:00:00+00:00")

    result = audit_paths(*sources(tmp_path, mutate))
    assert result["date_quality"]["event_clock_disagreements"] == 1
    assert result["date_quality"]["positive_missing_fix_date"] == 1
    assert result["date_quality"]["fix_before_author_assuming_utc"] == 1
    assert not result["historical_replay_ready"]


def test_malformed_date_rejected(tmp_path):
    with pytest.raises(PublicDataError, match="PUBLIC_FIX_DATE_INVALID"):
        audit_paths(*sources(tmp_path, lambda _, dates: dates[0].update(fix_date="bad")))


@pytest.mark.parametrize("remove_from", [0, 1])
def test_join_requires_exact_same_population(tmp_path, remove_from):
    def mutate(total, dates):
        (total, dates)[remove_from].pop()

    with pytest.raises(PublicDataError, match="PUBLIC_JOIN_(MISSING|EXTRA)"):
        audit_paths(*sources(tmp_path, mutate))


def test_same_time_groups_stable_and_not_optimized_for_labels():
    samples = [(i // 3, f"{i:040x}", i % 2 == 0) for i in range(30)]
    result = partition(samples)
    assert result == partition(list(reversed(samples)))
    splits = result["splits"]
    assert splits["train"]["last_event"] < splits["validation"]["first_event"]
    assert splits["validation"]["last_event"] < splits["test"]["first_event"]
    assert sum(v["rows"] for v in splits.values()) == 30
    assert partition([(1, "a", True)])["deficits"] == ["insufficient_time_groups"]


def test_single_class_and_empty_inputs_not_ready(tmp_path):
    def mutate(total, dates):
        for row in total + dates:
            row["buggy"] = "False"

    result = audit_paths(*sources(tmp_path, mutate))
    assert not result["published_benchmark_structurally_ready"]
    assert result["projects"]["apache/example"]["deficits"] == [
        "train_missing_class",
        "validation_missing_class",
        "test_missing_class",
    ]
    assert not audit_paths(*sources(tmp_path, count=0))["published_benchmark_structurally_ready"]


def test_limits_and_schema_are_enforced(tmp_path):
    paths = sources(tmp_path)
    for kwargs, code in (
        ({"max_rows": 2}, "ROW_LIMIT"),
        ({"max_bytes": 1}, "FILE_LIMIT"),
        ({"seconds": float("nan")}, "LIMIT_INVALID"),
    ):
        with pytest.raises(PublicDataError, match="PUBLIC_" + code):
            audit_paths(*paths, **kwargs)
    paths[0].write_text("wrong,columns\n1,2\n", encoding="utf-8")
    with pytest.raises(PublicDataError, match="PUBLIC_SCHEMA_INVALID"):
        audit_paths(*paths)


def test_sources_unchanged_and_content_bound(tmp_path):
    paths = sources(tmp_path)
    before = [p.read_bytes() for p in paths]
    first = audit_paths(*paths)
    assert [p.read_bytes() for p in paths] == before
    second = audit_paths(*sources(tmp_path, lambda rows, _: rows[0].update(la="2")))
    assert first["content_hash"] != second["content_hash"]


def test_timeout_uses_deterministic_supervision(tmp_path, monkeypatch):
    paths = sources(tmp_path)
    ticks = iter([0, 100])
    monkeypatch.setattr("packages.mining.public_dataset.time.perf_counter", lambda: next(ticks))
    with pytest.raises(PublicDataError, match="PUBLIC_TIMEOUT"):
        audit_paths(*paths, seconds=10)


def test_upstream_checksums_detect_tampering(tmp_path):
    path = tmp_path / "input.csv"
    path.write_bytes(b"real-source-bytes")
    raw = path.read_bytes()
    git = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    assert checksums(path, len(raw), "git-blob", git) == hashlib.sha256(raw).hexdigest()
    assert checksums(path, len(raw), "md5", hashlib.md5(raw).hexdigest())
    path.write_bytes(raw + b"x")
    with pytest.raises(ValueError, match="integrity mismatch"):
        checksums(path, len(raw), "git-blob", git)


@pytest.mark.parametrize(
    "url",
    [
        "http://zenodo.org/file",
        "https://localhost/file",
        "https://zenodo.org:8443/file",
        "https://user@zenodo.org/file",
    ],
)
def test_download_does_not_accept_arbitrary_destinations(url):
    with pytest.raises(ValueError, match="Unapproved"):
        validate_url(url)


def test_release_license_and_size_are_verified():
    metadata = {
        "id": 5907002,
        "metadata": {"license": {"id": "cc-by-4.0"}},
        "files": [
            {
                "key": "apachejit_total.csv",
                "size": 16467692,
                "checksum": "md5:98e102741020b3b13798d1e10a350689",
            }
        ],
    }
    verify_metadata(metadata)
    metadata["metadata"]["license"]["id"] = "unknown"
    with pytest.raises(ValueError, match="metadata mismatch"):
        verify_metadata(json.loads(json.dumps(metadata)))


def test_extra_csv_columns_do_not_silently_disappear(tmp_path):
    paths = sources(tmp_path)
    with paths[0].open("a", encoding="utf-8") as stream:
        stream.write("1,2,3\n")
    with pytest.raises(PublicDataError, match="PUBLIC_ROW_INVALID"):
        audit_paths(*paths)


def test_file_changed_during_audit_is_rejected(tmp_path, monkeypatch):
    from packages.mining import public_dataset

    paths = sources(tmp_path)
    original = public_dataset.fingerprint
    calls = 0

    def changed(path, **kwargs):
        nonlocal calls
        calls += 1
        value = original(path, **kwargs)
        return "f" * 64 if calls == 3 else value

    monkeypatch.setattr(public_dataset, "fingerprint", changed)
    with pytest.raises(PublicDataError, match="PUBLIC_SOURCE_CHANGED"):
        audit_paths(*paths)


def test_project_class_balance_is_not_hidden_by_pooling(tmp_path):
    def mutate(total, dates):
        for rows in (total, dates):
            for row in rows:
                if row["buggy"] == "True":
                    row["project"] = "apache/only-positive"

    result = audit_paths(*sources(tmp_path, mutate))
    assert result["project_groups"] == 2
    assert not result["published_benchmark_structurally_ready"]
    assert result["structurally_eligible_projects"] == []


def test_manifest_is_verified_not_trusted(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from scripts import fetch_public_dataset as fetch

    path = tmp_path / "input.csv"
    path.write_bytes(b"source")
    checksum = hashlib.md5(b"source").hexdigest()
    monkeypatch.setattr(
        fetch, "SOURCES", (("input.csv", "https://zenodo.org/source", 6, "md5", checksum),)
    )
    metadata = {
        "id": 5907002,
        "metadata": {"license": {"id": "cc-by-4.0"}},
        "files": [{"key": "input.csv", "size": 6, "checksum": "md5:" + checksum}],
    }
    (tmp_path / "zenodo-record.json").write_text(json.dumps(metadata), encoding="utf-8")
    manifest = {
        "acquired_at": datetime.now(UTC).isoformat(),
        "zenodo_record": 5907002,
        "apachejit_license": "CC-BY-4.0",
        "neurojit_revision": fetch.REVISION,
        "license_source": "https://zenodo.org/api/records/5907002",
        "license_metadata_sha256": hashlib.sha256(
            (tmp_path / "zenodo-record.json").read_bytes()
        ).hexdigest(),
        "upstream_code_executed": False,
        "files": [
            {
                "name": "input.csv",
                "url": "https://zenodo.org/source",
                "bytes": 6,
                "sha256": hashlib.sha256(b"source").hexdigest(),
            }
        ],
    }
    manifest_path = tmp_path / "acquisition.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert fetch.verify_manifest(tmp_path) == manifest
    manifest["files"][0]["url"] = "https://unverified.example/source"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest mismatch"):
        fetch.verify_manifest(tmp_path)


def test_runtime_boundary_and_output_cannot_fall_back_to_other_disk(tmp_path, monkeypatch):
    from scripts import fetch_public_dataset as fetch

    monkeypatch.setattr(fetch, "ROOT", tmp_path)
    assert fetch.runtime_path(tmp_path / "runtime/data") == tmp_path / "runtime/data"
    with pytest.raises(ValueError, match="project runtime"):
        fetch.runtime_path(tmp_path / "outside/data")
