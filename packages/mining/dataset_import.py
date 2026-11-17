"""User-imported public datasets: normalize, seal, reuse the asset pipeline.

An imported file is normalized into the same sealed-asset shape as the built-in
apachejit asset (source CSV in TOTAL_FIELDS + DATE_FIELDS, acquisition receipt,
normalized.jsonl, quality.json). ``research_dataset.prepare`` then consumes it
unchanged except for the import-specific acquisition semantics.
"""

import csv
import io
import json
import math
import os
import shutil
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from packages.mining import public_assets as assets
from packages.mining import public_dataset
from packages.mining.public_dataset import DATE_FIELDS, NUMERIC_FIELDS, TOTAL_FIELDS
from packages.mining.source_evidence import canonical, content_hash

# 上传与本地路径共用的导入上限：比内置资产（20 万行 / 256 MB）更紧。
IMPORT_LIMIT_ROWS = 20000
IMPORT_LIMIT_BYTES = 32 * 1024**2

IDENTITY_FIELDS = ("canonical_repository", "commit_id", "event_epoch", "label")
LICENSE_OPTIONS = ("CC-BY-4.0", "CC0-1.0", "Apache-2.0", "MIT", "OTHER")
MAPPABLE_FIELDS = (*IDENTITY_FIELDS, *NUMERIC_FIELDS, "fix")

LABEL_VALUES = {"0": "False", "1": "True", "True": "True", "False": "False"}


class ImportError(ValueError):
    """Stable safe import errors; messages never include upstream row contents."""

    def __init__(self, code, message=""):
        self.code = code
        self.message = message
        super().__init__(code)


def import_key(row):
    """Identity key for imported rows; open formats, no apache-only assumptions."""
    commit = row["commit_id"].strip()
    repository = row["project"].strip()
    if not commit or len(commit) > 64 or any(ord(c) < 32 for c in commit):
        raise public_dataset.PublicDataError("PUBLIC_KEY_INVALID")
    if not repository or len(repository) > 128 or any(ord(c) < 32 for c in repository):
        raise public_dataset.PublicDataError("PUBLIC_KEY_INVALID")
    return repository, commit


def parse_columns(columns):
    """Validate the user-declared mapping {field: column}. v1 requires the four
    identity columns and all 12 numeric features; fix is optional."""
    if not isinstance(columns, dict) or not columns:
        raise ImportError("DATASET_IMPORT_COLUMNS_INVALID", "缺少列映射")
    unknown = sorted(set(columns) - set(MAPPABLE_FIELDS))
    if unknown:
        raise ImportError(
            "DATASET_IMPORT_COLUMNS_INVALID", "未知字段: " + ", ".join(unknown)
        )
    missing = [f for f in (*IDENTITY_FIELDS, *NUMERIC_FIELDS) if not columns.get(f)]
    if missing:
        raise ImportError(
            "DATASET_IMPORT_COLUMNS_INVALID", "缺少必填列: " + ", ".join(missing)
        )
    if any(
        type(value) is not str or not value.strip() or len(value) > 128
        for value in columns.values()
    ):
        raise ImportError("DATASET_IMPORT_COLUMNS_INVALID", "列名必须是 1~128 字符的文本")
    stripped = [value.strip() for value in columns.values()]
    if len(set(stripped)) != len(stripped):
        raise ImportError(
            "DATASET_IMPORT_COLUMNS_INVALID", "同一文件列被映射到多个字段"
        )
    return {field: columns[field].strip() for field in MAPPABLE_FIELDS if field in columns}


def _label(value, number):
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int) and value in (0, 1):
        return "True" if value else "False"
    if isinstance(value, str) and value in LABEL_VALUES:
        return LABEL_VALUES[value]
    raise ImportError(
        "DATASET_IMPORT_ROW_INVALID", f"第 {number} 行 label 只能是 0/1/True/False"
    )


def _epoch_value(value, number):
    if isinstance(value, bool):
        raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行 event_epoch 不合法")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float) and value.is_integer():
        text = str(int(value))
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行 event_epoch 不合法")
    try:
        return public_dataset.epoch(text)
    except public_dataset.PublicDataError:
        raise ImportError(
            "DATASET_IMPORT_ROW_INVALID", f"第 {number} 行 event_epoch 必须是 0~4102444800 的整数"
        ) from None


def _numeric(value, number):
    if isinstance(value, bool):
        raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行数值特征不合法")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行数值特征不合法") from None
    if not math.isfinite(result) or result < 0:
        raise ImportError(
            "DATASET_IMPORT_ROW_INVALID", f"第 {number} 行数值特征必须是大于等于 0 的有限数"
        )
    return result


def _identity_text(value, field, number, maximum):
    if type(value) is not str:
        raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行 {field} 不合法")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(c) < 32 for c in text):
        raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行 {field} 不合法")
    return text


def _fix_value(value, number):
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str) and value in ("True", "False"):
        return value
    raise ImportError(
        "DATASET_IMPORT_ROW_INVALID", f"第 {number} 行 fix 只能是 True/False"
    )


def parse_payload(data, mapping, *, tick=None):
    """Parse CSV or JSONL bytes into TOTAL_FIELDS rows plus per-row facts.

    Returns (rows, facts) where rows are the source-CSV dicts and facts carry
    parsed epoch/label/numeric/fix values for the normalized records.
    """
    if not isinstance(data, bytes) or not data:
        raise ImportError("DATASET_IMPORT_FILE_INVALID", "文件为空")
    if len(data) > IMPORT_LIMIT_BYTES:
        raise ImportError(
            "DATASET_IMPORT_LIMIT",
            f"文件超过导入上限（{IMPORT_LIMIT_BYTES // 1024**2} MB）",
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError("DATASET_IMPORT_FILE_INVALID", "文件不是 UTF-8 编码") from None
    budget = assets.Budget(tick=tick, seconds=60)
    rows = []
    facts = []
    fix_mapped = "fix" in mapping
    if text.lstrip("﻿ \t\r\n").startswith("{"):
        for number, line in enumerate(text.splitlines(), 1):
            budget.check()
            if number > IMPORT_LIMIT_ROWS:
                raise ImportError("DATASET_IMPORT_LIMIT", "行数超过导入上限")
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行不是合法 JSON") from None
            if not isinstance(value, dict):
                raise ImportError("DATASET_IMPORT_ROW_INVALID", f"第 {number} 行不是 JSON 对象")
            rows.append(_normalize_row(value, mapping, number, fix_mapped))
            facts.append(_row_facts(value, mapping, number, fix_mapped))
            budget.check()
    else:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ImportError("DATASET_IMPORT_FILE_INVALID", "CSV 表头缺失或有重复列名")
        missing_columns = [
            column for column in set(mapping.values()) if column not in reader.fieldnames
        ]
        if missing_columns:
            raise ImportError(
                "DATASET_IMPORT_COLUMNS_INVALID",
                "文件里没有这些列: " + ", ".join(sorted(missing_columns)),
            )
        for number, row in enumerate(reader, 1):
            budget.check()
            if number > IMPORT_LIMIT_ROWS:
                raise ImportError("DATASET_IMPORT_LIMIT", "行数超过导入上限")
            rows.append(_normalize_row(row, mapping, number, fix_mapped))
            facts.append(_row_facts(row, mapping, number, fix_mapped))
    if not rows:
        raise ImportError("DATASET_IMPORT_FILE_INVALID", "文件里没有数据行")
    return rows, facts


def _normalize_row(row, mapping, number, fix_mapped):
    fix = _fix_value(row.get(mapping["fix"]), number) if fix_mapped else ""
    values = {field: _numeric(row.get(mapping[field]), number) for field in NUMERIC_FIELDS}
    return {
        "commit_id": _identity_text(row.get(mapping["commit_id"]), "commit_id", number, 64),
        "project": _identity_text(
            row.get(mapping["canonical_repository"]), "canonical_repository", number, 128
        ),
        "buggy": _label(row.get(mapping["label"]), number),
        "fix": fix,
        "year": "",
        "author_date": str(_epoch_value(row.get(mapping["event_epoch"]), number)),
        **{field: str(values[field]) for field in NUMERIC_FIELDS},
    }


def _row_facts(row, mapping, number, fix_mapped):
    values = {field: _numeric(row.get(mapping[field]), number) for field in NUMERIC_FIELDS}
    return {
        "event_epoch": _epoch_value(row.get(mapping["event_epoch"]), number),
        "buggy": _label(row.get(mapping["label"]), number) == "True",
        "numeric12": values,
        "fix": _fix_value(row.get(mapping["fix"]), number) == "True" if fix_mapped else None,
    }


def seal_asset(store, rows, facts, mapping, license, *, tick=None):
    """Seal normalized rows into an asset directory; idempotent by content."""
    store = assets.safe_path(store)
    store.mkdir(parents=True, exist_ok=True)
    budget = assets.Budget(tick=tick, seconds=120)
    budget.check()
    fix_mapped = "fix" in mapping
    # 不落 acquired_at：导入的获取时间就是 first_stored_at，且时间戳进 identity
    # 会破坏「同文件同映射同许可 → 同一资产」的幂等键。
    acquisition = {
        "source_kind": "user_import",
        "upstream_code_executed": False,
        "import_license_declaration": license,
        "import_columns": mapping,
        "fix_column_mapped": fix_mapped,
    }
    rule = content_hash(
        {
            p.name: p.read_text(encoding="utf-8").replace("\r\n", "\n")
            for p in (Path(__file__), Path(assets.__file__))
        }
    )
    attempt = assets.safe_path(store / (".staging-" + uuid.uuid4().hex))
    attempt.mkdir()
    try:
        (attempt / "source").mkdir()
        with (attempt / "source/apachejit_total.csv").open(
            "x", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=TOTAL_FIELDS)
            writer.writeheader()
            for row in rows:
                budget.check()
                writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())
        with (attempt / "source/apachejit_date.csv").open(
            "x", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=DATE_FIELDS)
            writer.writeheader()
            for row in rows:
                budget.check()
                writer.writerow(
                    {
                        "commit_id": row["commit_id"],
                        "project": row["project"],
                        "bug_date": "",
                        "fix_date": "",
                        "buggy": row["buggy"],
                        "fix": row["fix"],
                        "date": "",
                        "year": "",
                    }
                )
            stream.flush()
            os.fsync(stream.fileno())
        assets.write_json(attempt / "source/acquisition.json", acquisition)
        labels = Counter()
        cohorts = set()
        with (attempt / "normalized.jsonl").open("xb") as dst:
            size = 0
            for number, (row, facts_row) in enumerate(zip(rows, facts, strict=True), 1):
                budget.check()
                record = {
                    "row_number": number,
                    "cohort": row["project"],
                    "canonical_repository": row["project"],
                    "commit_id": row["commit_id"],
                    "sample_granularity": "commit",
                    "original_fields": row,
                    "event_epoch": facts_row["event_epoch"],
                    "event_clock": "user_declared_event_epoch_UTC",
                    "published_label": "buggy" if facts_row["buggy"] else "clean",
                    "label_confidence": "user_declared_import_label",
                    "label_available_at": None,
                    "numeric12": facts_row["numeric12"],
                    "published_fix_metadata": facts_row["fix"],
                    "defect_types": None,
                    "type_status": "unknown",
                    "location": None,
                    "languages": [],
                    "language_status": "per_row_unverified",
                    "semantic_available": False,
                }
                raw = canonical(record) + b"\n"
                size += len(raw)
                if len(raw) > 16384 or size > assets.MAX_BYTES:
                    raise assets.AssetError("ASSET_BYTE_LIMIT")
                dst.write(raw)
                labels[record["published_label"]] += 1
                cohorts.add(row["project"])
                budget.check()
            dst.flush()
            os.fsync(dst.fileno())
        quality = assets.sealed(
            {
                "version": assets.VERSION,
                "rows": len(rows),
                "labels": dict(labels),
                "public_audit": {
                    "source": "user_import",
                    "rows": len(rows),
                    "labels": dict(labels),
                },
                "numeric_features": list(NUMERIC_FIELDS),
                "fix_is_metadata_not_product_FIX": True,
                "LT_synthesized": False,
                "capabilities": assets.CAPABILITIES,
                "research_input_eligible_cohorts": sorted(cohorts),
                "language_scope": "user_declared_import_unverified",
                "unknown_types": len(rows),
                "missing_semantic_rows": len(rows),
                "negative_coverage": "unknown",
                "gold_labels_certified": False,
            }
        )
        assets.write_json(attempt / "quality.json", quality)
        inventory = [
            {
                "path": "source/apachejit_total.csv",
                **assets.file_digest(attempt / "source/apachejit_total.csv", budget.check),
            },
            {
                "path": "source/apachejit_date.csv",
                **assets.file_digest(attempt / "source/apachejit_date.csv", budget.check),
            },
            {
                "path": "source/acquisition.json",
                **assets.file_digest(attempt / "source/acquisition.json", budget.check),
            },
            {
                "path": "normalized.jsonl",
                **assets.file_digest(attempt / "normalized.jsonl", budget.check),
            },
            {
                "path": "quality.json",
                **assets.file_digest(attempt / "quality.json", budget.check),
            },
        ]
        if sum(record["bytes"] for record in inventory) > assets.MAX_BYTES:
            raise assets.AssetError("ASSET_BYTE_LIMIT")
        identity = {
            "adapter_version": assets.VERSION,
            "adapter_rule_hash": rule,
            "source_acquisition": acquisition,
            "files": inventory,
        }
        asset_id = content_hash(identity)
        manifest = assets.sealed(
            {
                "asset_id": asset_id,
                "identity": identity,
                "first_stored_at": datetime.now(UTC).isoformat(),
            }
        )
        assets.write_json(attempt / "manifest.json", manifest)
        target = assets.safe_path(store / asset_id)
        budget.check()
        if target.exists():
            return assets.load_asset(target, tick=budget.check)
        try:
            os.rename(attempt, target)
        except OSError:
            if target.exists():
                return assets.load_asset(target, tick=budget.check)
            raise
        return assets.load_asset(target, tick=budget.check)
    finally:
        if attempt.exists():
            assets.safe_path(attempt)
            shutil.rmtree(attempt)


def detect_columns(path, *, tick=None):
    """Read the first line of a server-local file and list its columns."""
    source = Path(path)
    try:
        source = source.resolve()
    except OSError:
        raise ImportError("DATASET_IMPORT_PATH_INVALID", "路径不可用") from None
    if not source.is_file():
        raise ImportError("DATASET_IMPORT_PATH_INVALID", "路径不可用")
    budget = assets.Budget(tick=tick, seconds=30)
    with source.open("rb") as stream:
        head = stream.read(65536)
    budget.check()
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError("DATASET_IMPORT_FILE_INVALID", "文件不是 UTF-8 编码") from None
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first.startswith("{"):
        try:
            value = json.loads(first)
        except json.JSONDecodeError:
            raise ImportError("DATASET_IMPORT_FILE_INVALID", "第一行不是合法 JSON") from None
        if not isinstance(value, dict):
            raise ImportError("DATASET_IMPORT_FILE_INVALID")
        return list(value)
    return [column for column in first.split(",")]


def import_payload(store, payload, *, columns, license, tick=None):
    """Bytes entry: parse, validate, seal. Returns (manifest, quality)."""
    if license not in LICENSE_OPTIONS:
        raise ImportError(
            "DATASET_IMPORT_LICENSE_INVALID", "许可声明必须是 " + "/".join(LICENSE_OPTIONS)
        )
    mapping = parse_columns(columns)
    rows, facts = parse_payload(payload, mapping, tick=tick)
    return seal_asset(store, rows, facts, mapping, license, tick=tick)


def import_path(store, path, *, columns, license, tick=None):
    """Server-local path entry (Admin only); same sealed pipeline as uploads."""
    if license not in LICENSE_OPTIONS:
        raise ImportError(
            "DATASET_IMPORT_LICENSE_INVALID", "许可声明必须是 " + "/".join(LICENSE_OPTIONS)
        )
    mapping = parse_columns(columns)
    source = Path(path)
    try:
        source = source.resolve()
    except OSError:
        raise ImportError("DATASET_IMPORT_PATH_INVALID", "路径不可用") from None
    if not source.is_file() or source.stat().st_size > IMPORT_LIMIT_BYTES:
        raise ImportError("DATASET_IMPORT_PATH_INVALID", "路径不可用或文件超过导入上限")
    budget = assets.Budget(tick=tick, seconds=120)
    with source.open("rb") as stream:
        payload = stream.read(IMPORT_LIMIT_BYTES + 1)
    budget.check()
    rows, facts = parse_payload(payload, mapping, tick=tick)
    return seal_asset(store, rows, facts, mapping, license, tick=tick)
