"""上传的提交级逗号分隔值：显式列映射后走既有资产管线。

服务端不猜列，也不补零：十六个必填字段缺一个就拒绝，缺特征行的情况留到数据管线
支持缺失值之后再处理。
"""

import csv
import hashlib
import io
import math
import shutil
from datetime import UTC, datetime
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.public_dataset import NUMERIC_FIELDS, TOTAL_FIELDS

MAX_TEXT_BYTES = 4 * 1024**2
MAX_ROWS = 20000
REQUIRED = ("commit_id", "project", "buggy", "author_date", *NUMERIC_FIELDS)
LABELS = {"True": True, "False": False, "1": True, "0": False}


class UploadError(ValueError):
    """稳定的导入错误；消息里不带源数据内容。"""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _epoch(value, number):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        raise UploadError("PUBLIC_TIME_INVALID", f"row {number}") from None
    if parsed < 0:
        raise UploadError("PUBLIC_TIME_INVALID", f"row {number}")
    return parsed


def _number(value, number):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise UploadError("PUBLIC_NUMERIC_INVALID", f"row {number}") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise UploadError("PUBLIC_NUMERIC_INVALID", f"row {number}")
    return parsed


def _mapping_columns(mapping, header):
    missing = [field for field in REQUIRED if not str(mapping.get(field, "")).strip()]
    if missing:
        raise UploadError("PUBLIC_MAPPING_INCOMPLETE", ",".join(missing))
    columns = [str(mapping[field]).strip() for field in REQUIRED]
    if len(set(columns)) != len(columns):
        raise UploadError("PUBLIC_MAPPING_DUPLICATE")
    unknown = [column for column in columns if column not in header]
    if unknown:
        raise UploadError("PUBLIC_MAPPING_UNKNOWN", ",".join(unknown))
    return {field: column for field, column in zip(REQUIRED, columns, strict=True)}


def read_upload(content):
    """校验体积并从文本里取出表头与行迭代器。"""
    if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise UploadError("PUBLIC_BYTE_LIMIT")
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline=""))
    header = [name.strip() for name in (reader.fieldnames or [])]
    if not header:
        raise UploadError("PUBLIC_SCHEMA_INVALID", "header")
    return reader, header


def _rows(reader, columns):
    rows = []
    seen = set()
    for number, row in enumerate(reader, start=1):
        if number > MAX_ROWS:
            raise UploadError("PUBLIC_ROW_LIMIT")
        values = {name: (row.get(source) or "").strip() for name, source in columns.items()}
        commit, project = values["commit_id"], values["project"]
        if not commit or not project:
            raise UploadError("PUBLIC_FIELD_INVALID", f"row {number}")
        if (project, commit) in seen:
            raise UploadError("PUBLIC_DUPLICATE_SAMPLE", f"row {number}")
        seen.add((project, commit))
        label = LABELS.get(values["buggy"])
        if label is None:
            raise UploadError("PUBLIC_LABEL_INVALID", f"row {number}")
        epoch = _epoch(values["author_date"], number)
        numbers = {name: _number(values[name], number) for name in NUMERIC_FIELDS}
        rows.append(
            dict(
                {
                    "commit_id": commit,
                    "project": project,
                    "buggy": "True" if label else "False",
                    "fix": "False",
                    "year": str(datetime.fromtimestamp(epoch, UTC).year),
                    "author_date": str(epoch),
                },
                **{name: repr(numbers[name]) for name in NUMERIC_FIELDS},
            )
        )
    if not rows:
        raise UploadError("PUBLIC_ROW_LIMIT", "0")
    return rows


def _existing(store, content_sha256, license, source_name):
    """按内容与声明找回已导入的资产，重复导入返回同一标识。"""
    if not store.is_dir():
        return None
    for directory in sorted(store.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        try:
            loaded = assets.load_asset(directory)
        except (assets.AssetError, OSError, ValueError):
            continue
        acquisition = loaded["manifest"]["identity"]["source_acquisition"]
        if (
            acquisition.get("source_kind") == "upload"
            and acquisition.get("content_sha256") == content_sha256
            and acquisition.get("license") == license
            and acquisition.get("source_name") == source_name
        ):
            return loaded
    return None


def _summary(loaded):
    manifest, quality = loaded["manifest"], loaded["quality"]
    return {
        "asset_id": manifest["asset_id"],
        "source_hash": manifest["content_hash"],
        "rows": quality["rows"],
        "labels": quality["labels"],
    }


def import_upload(
    content,
    mapping,
    *,
    license,
    source_name,
    user_id,
    store=None,
    acquired_at=None,
    tick=None,
):
    reader, header = read_upload(content)
    columns = _mapping_columns(mapping, header)
    rows = _rows(reader, columns)
    store = store or assets.ROOT / "runtime/public-assets"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    found = _existing(store, content_sha256, license, source_name)
    if found is not None:
        return _summary(found)
    workspace = assets.ROOT / "runtime/temp" / ("upload-" + uuid4().hex)
    workspace.mkdir(parents=True)
    try:
        published = workspace / "published"
        published.mkdir()
        with (published / "upload.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(TOTAL_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        acquisition = {
            "acquired_at": acquired_at or datetime.now(UTC).isoformat(),
            "source_kind": "upload",
            "source_name": source_name,
            "license": license,
            "content_sha256": content_sha256,
            "uploaded_by": user_id,
            "upstream_code_executed": False,
        }
        assets.write_json(published / "acquisition.json", acquisition)
        loaded = assets.import_asset(
            published,
            store,
            verify_source=lambda _: acquisition,
            tick=tick,
            kind="upload",
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return _summary(loaded)
