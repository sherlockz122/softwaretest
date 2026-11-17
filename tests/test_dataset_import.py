"""User-imported public datasets: mapping, normalization, sealing, idempotency."""

import csv
import io
import json

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import dataset_import, public_assets as assets, research_dataset
from packages.mining.dataset_import import IMPORT_LIMIT_BYTES, IMPORT_LIMIT_ROWS, ImportError
from packages.mining.public_dataset import NUMERIC_FIELDS

MAPPING = {
    "canonical_repository": "repo",
    "commit_id": "sha",
    "event_epoch": "time",
    "label": "bug",
    **{name: name for name in NUMERIC_FIELDS},
    "fix": "fix",
}
LICENSE = "CC-BY-4.0"
POLICY = {
    "purpose": "published_binary_12_v1",
    "split_strategy": "time_ordered",
    "train_ratio": 0.7,
    "validation_ratio": 0,
    "test_ratio": 0.3,
}


def sample_csv(count=40, projects=("user/alpha", "user/beta"), fix=True):
    header = ["repo", "sha", "time", "bug", *NUMERIC_FIELDS, *(["fix"] if fix else [])]
    rows = []
    n = 0
    for project in projects:
        for i in range(1, count + 1):
            n += 1
            row = [project, f"{n:040x}", str(1000 * n), "1" if n % 2 else "0"]
            row += [str(n) for _ in NUMERIC_FIELDS]
            if fix:
                row.append("True" if n % 3 == 0 else "False")
            rows.append(row)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def sample_jsonl(count=40, projects=("user/alpha", "user/beta"), fix=True):
    lines = []
    n = 0
    for project in projects:
        for i in range(1, count + 1):
            n += 1
            record = {
                "repo": project,
                "sha": f"{n:040x}",
                "time": 1000 * n,
                "bug": n % 2,
                **{name: float(n) for name in NUMERIC_FIELDS},
            }
            if fix:
                record["fix"] = n % 3 == 0
            lines.append(json.dumps(record, sort_keys=True))
    return ("\n".join(lines) + "\n").encode("utf-8")


def import_fixture(work, payload, mapping=MAPPING, license=LICENSE):
    store = work / "store"
    loaded = dataset_import.import_payload(store, payload, columns=mapping, license=license)
    return store / loaded["manifest"]["asset_id"], loaded["manifest"], loaded["quality"]


@pytest.fixture
def work():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="import-unit-") as directory:
        yield Path(directory)


def test_mapping_valid_and_fix_optional():
    parsed = dataset_import.parse_columns(MAPPING)
    assert set(parsed) == set(dataset_import.MAPPABLE_FIELDS)
    assert parsed["fix"] == "fix"
    without_fix = {k: v for k, v in MAPPING.items() if k != "fix"}
    parsed = dataset_import.parse_columns(without_fix)
    assert "fix" not in parsed


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda m: m.pop("commit_id"), "commit_id"),
        (lambda m: m.pop("la"), "la"),
        (lambda m: m.update({"la": 123}), "列名必须是"),
        (lambda m: m.update({"unknown_field": "x"}), "未知字段"),
        (lambda m: m.update({"la": "la", "ld": "la"}), "同一文件列被映射到多个字段"),
    ],
)
def test_columns_invalid(mutate, fragment):
    mapping = dict(MAPPING)
    mutate(mapping)
    with pytest.raises(ImportError) as excinfo:
        dataset_import.parse_columns(mapping)
    assert excinfo.value.code == "DATASET_IMPORT_COLUMNS_INVALID"
    assert fragment in excinfo.value.message


def test_csv_import_seals_and_prepare_consumes(work):
    directory, manifest, quality = import_fixture(work, sample_csv())
    assert directory.exists() and directory.name == manifest["asset_id"]
    assert quality["rows"] == 80
    assert quality["capabilities"]["normalized"] is True
    preview = research_dataset.preview(manifest["asset_id"], store=directory.parent)
    assert preview["source_kind"] == "user_import"
    assert preview["rows"] == 80
    members, metadata, plan_hash = research_dataset.prepare(
        directory, manifest["content_hash"], POLICY
    )
    assert len(members) == 80
    assert metadata["source_acquisition"]["source_kind"] == "user_import"
    assert metadata["source_acquisition"]["import_license_declaration"] == LICENSE
    assert metadata["target"] == "user_declared_import_label"
    assert metadata["event_clock"] == "user_declared_event_epoch_UTC"
    assert "fix_unavailable_in_imported_source" not in metadata["limitations"]
    assert all(m["fix"] is not None for m in members)
    assert {m["lt"] for m in members} == {None}
    assert all(m["lt_missing_reason"] == "upstream_source_omits_lt" for m in members)
    assert sorted(m["partition"] for m in members) == ["test"] * 24 + ["train"] * 56


def test_fix_unmapped_members_carry_reason(work):
    mapping = {k: v for k, v in MAPPING.items() if k != "fix"}
    directory, manifest, _ = import_fixture(work, sample_csv(fix=False), mapping)
    members, metadata, _ = research_dataset.prepare(
        directory, manifest["content_hash"], POLICY
    )
    assert all(m["fix"] is None for m in members)
    assert all(m["fix_missing_reason"] == "import_source_omits_fix" for m in members)
    assert "fix_unavailable_in_imported_source" in metadata["limitations"]


def test_jsonl_import_matches_csv_shape(work):
    directory, manifest, quality = import_fixture(work, sample_jsonl())
    assert quality["rows"] == 80
    members, metadata, _ = research_dataset.prepare(
        directory, manifest["content_hash"], POLICY
    )
    assert len(members) == 80
    assert all(m["fix"] is not None for m in members)


def test_idempotent_same_payload_same_asset(work):
    store = work / "store"
    first = dataset_import.import_payload(
        store, sample_csv(), columns=MAPPING, license=LICENSE
    )
    second = dataset_import.import_payload(
        store, sample_csv(), columns=MAPPING, license=LICENSE
    )
    assert first["manifest"]["asset_id"] == second["manifest"]["asset_id"]
    assert [p.name for p in store.iterdir()] == [first["manifest"]["asset_id"]]


def test_different_mapping_different_asset(work):
    store = work / "store"
    mapping = {k: v for k, v in MAPPING.items() if k != "fix"}
    first = dataset_import.import_payload(
        store, sample_csv(), columns=MAPPING, license=LICENSE
    )
    second = dataset_import.import_payload(
        store, sample_csv(fix=False), columns=mapping, license=LICENSE
    )
    assert first["manifest"]["asset_id"] != second["manifest"]["asset_id"]


def _full_header():
    return "repo,sha,time,bug," + ",".join(NUMERIC_FIELDS) + ",fix"


def _full_line(parts):
    return ",".join(parts[:5] + ["1"] * 11 + ["False"])


@pytest.mark.parametrize(
    "bad_parts, fragment",
    [
        (["user/a", "abc", "1000", "2", "1"], "label 只能是"),
        (["user/a", "abc", "not-a-time", "1", "1"], "event_epoch"),
        (["user/a", "abc", "1000", "1", "-1"], "数值特征"),
    ],
)
def test_bad_rows_rejected(work, bad_parts, fragment):
    payload = (_full_header() + "\n" + _full_line(bad_parts) + "\n").encode()
    with pytest.raises(ImportError) as excinfo:
        dataset_import.import_payload(
            work / "store", payload, columns=MAPPING, license=LICENSE
        )
    assert excinfo.value.code == "DATASET_IMPORT_ROW_INVALID"
    assert fragment in excinfo.value.message


def test_tampered_source_rejected_by_prepare(work):
    directory, manifest, _ = import_fixture(work, sample_csv())
    source = directory / "source/apachejit_total.csv"
    source.write_bytes(source.read_bytes() + b"\n# tampered\n")
    # 篡改最先被资产层的清单摘要校验拦截（比 prepare 的快照检查更早）。
    with pytest.raises(assets.AssetError) as excinfo:
        research_dataset.prepare(directory, manifest["content_hash"], POLICY)
    assert str(excinfo.value) == "ASSET_FILE_DRIFT"


def test_duplicate_samples_merge_in_prepare(work):
    lines = sample_csv(count=20, projects=("user/solo",)).decode().splitlines()
    payload = "\n".join([*lines, lines[1]]) + "\n"
    directory, manifest, _ = import_fixture(work, payload.encode())
    members, metadata, _ = research_dataset.prepare(
        directory, manifest["content_hash"], POLICY
    )
    assert metadata["original_rows"] == 21
    assert metadata["duplicate_rows"] == 1
    assert len(members) == 20
    merged = [m for m in members if len(m["source_rows"]) == 2]
    assert len(merged) == 1


def test_duplicate_identity_conflict_rejected(work):
    header = _full_header()
    line = lambda bug: (
        "user/solo," + "1" * 40 + f",1000,{bug}," + ",".join(["1"] * 12) + ",False"
    )
    payload = (header + "\n" + line("1") + "\n" + line("0") + "\n").encode()
    store = work / "store"
    loaded = dataset_import.import_payload(
        store, payload, columns=MAPPING, license=LICENSE
    )
    with pytest.raises(research_dataset.ResearchError) as excinfo:
        research_dataset.prepare(
            store / loaded["manifest"]["asset_id"],
            loaded["manifest"]["content_hash"],
            POLICY,
        )
    assert excinfo.value.code == "DATA_DUPLICATE_CONFLICT"


def test_row_limit_and_byte_limit(work):
    rows = [
        f"user/a,{i:040x},{1000 + i},1," + ",".join(["1"] * 12) + ",False"
        for i in range(IMPORT_LIMIT_ROWS + 1)
    ]
    header = _full_header()
    with pytest.raises(ImportError) as excinfo:
        dataset_import.import_payload(
            work / "store",
            (header + "\n" + "\n".join(rows) + "\n").encode(),
            columns=MAPPING,
            license=LICENSE,
        )
    assert excinfo.value.code == "DATASET_IMPORT_LIMIT"
    with pytest.raises(ImportError) as excinfo:
        dataset_import.import_payload(
            work / "store",
            b"x" * (IMPORT_LIMIT_BYTES + 1),
            columns=MAPPING,
            license=LICENSE,
        )
    assert excinfo.value.code == "DATASET_IMPORT_LIMIT"


def test_missing_header_column_and_duplicate_header(work):
    with pytest.raises(ImportError) as excinfo:
        dataset_import.import_payload(
            work / "store",
            b"repo,sha,time,bug\nuser/a,abc,1000,1\n",
            columns=MAPPING,
            license=LICENSE,
        )
    assert excinfo.value.code == "DATASET_IMPORT_COLUMNS_INVALID"
    duplicated = _full_header().replace(",la,", ",la,la,")
    with pytest.raises(ImportError) as excinfo:
        dataset_import.import_payload(
            work / "store",
            (duplicated + "\n" + _full_line(["user/a", "abc", "1000", "1", "1"]) + "\n").encode(),
            columns=MAPPING,
            license=LICENSE,
        )
    assert excinfo.value.code == "DATASET_IMPORT_FILE_INVALID"


def test_license_invalid(work):
    with pytest.raises(ImportError) as excinfo:
        dataset_import.import_payload(
            work / "store", sample_csv(), columns=MAPPING, license="WTFPL"
        )
    assert excinfo.value.code == "DATASET_IMPORT_LICENSE_INVALID"
