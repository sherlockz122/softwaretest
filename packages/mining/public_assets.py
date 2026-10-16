"""Bounded offline public assets; integrity is not label or publication eligibility."""

import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from packages.mining import public_dataset
from packages.mining.public_dataset import NUMERIC_FIELDS, TOTAL_FIELDS, boolean, epoch, key
from packages.mining.source_evidence import canonical, content_hash

ROOT = Path(__file__).resolve().parents[2]
VERSION = "apachejit-asset-12-v1"
MAX_BYTES = 256 * 1024**2
MAX_ROWS = 200000
MAX_SECONDS = 180
SOURCE_NAMES = {
    "apachejit_total.csv",
    "apachejit_date.csv",
    "acquisition.json",
    "zenodo-record.json",
    "neurojit-LICENSE.txt",
    "neurojit-data_utils.py.txt",
}
CAPABILITIES = {
    "raw_archived": True,
    "normalized": True,
    "research_dataset_ready": False,
    "research_semantic": False,
    "historical_replay": False,
    "candidate_release": False,
    "type_prediction": False,
    "localization": False,
}


class AssetError(ValueError):
    """Safe offline diagnostic, without source row contents or account details."""


def safe_path(value):
    path = Path(os.path.abspath(value))
    if not path.is_relative_to(ROOT / "runtime"):
        raise AssetError("ASSET_PATH_OUTSIDE_RUNTIME")
    for part in (path, *path.parents):
        if part.is_symlink() or (
            part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise AssetError("ASSET_PATH_LINK")
        if part == ROOT:
            break
    return path


class Budget:
    def __init__(self, *, seconds=MAX_SECONDS, tick=None):
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 < seconds <= MAX_SECONDS
        ):
            raise AssetError("ASSET_LIMIT_INVALID")
        self.deadline = time.monotonic() + seconds
        self.tick = tick

    def check(self):
        if self.tick:
            self.tick()
        if time.monotonic() > self.deadline:
            raise AssetError("ASSET_DEADLINE")
        if shutil.disk_usage(ROOT).free < 2 * 1024**3:
            raise AssetError("ASSET_CAPACITY_UNSAFE")


def file_digest(path, check, *, maximum=MAX_BYTES):
    path = safe_path(path)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise AssetError("ASSET_FILE_INVALID")
    digest, count = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check()
            count += len(chunk)
            if count > maximum:
                raise AssetError("ASSET_BYTE_LIMIT")
            digest.update(chunk)
    check()
    return {"bytes": count, "sha256": digest.hexdigest()}


def read_json(path, check, *, maximum=1024**2):
    path = safe_path(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise AssetError("ASSET_JSON_LIMIT")

    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise AssetError("ASSET_JSON_DUPLICATE")
            result[k] = v
        return result

    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise AssetError("ASSET_JSON_LIMIT")
        result = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(AssetError("ASSET_JSON_NUMBER")),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssetError("ASSET_JSON_INVALID") from exc
    check()
    return result


def utc_time(value):
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise AssetError("ASSET_TIME_INVALID") from exc
    if result.tzinfo is None or not datetime(2022, 1, 1, tzinfo=UTC) <= result <= datetime.now(UTC):
        raise AssetError("ASSET_TIME_INVALID")
    return result.astimezone(UTC).isoformat()


def sealed(value):
    result = json.loads(canonical(value))
    result["content_hash"] = content_hash(result)
    return result


def verify_seal(value):
    if not isinstance(value, dict) or value.get("content_hash") != content_hash(
        {k: v for k, v in value.items() if k != "content_hash"}
    ):
        raise AssetError("ASSET_SEAL_INVALID")


def write_json(path, value):
    with safe_path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def atomic_json(path, value, *, check):
    path = safe_path(path)
    if path.exists():
        raise AssetError("ASSET_OUTPUT_EXISTS")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        write_json(part, value)
        check()
        os.link(part, safe_path(path))
    finally:
        part.unlink(missing_ok=True)


def source_inventory(directory, check):
    records = []
    total = 0
    for name in sorted(SOURCE_NAMES):
        record = file_digest(directory / name, check)
        total += record["bytes"]
        if total > 100 * 1024**2:
            raise AssetError("ASSET_SOURCE_LIMIT")
        records.append({"path": "source/" + name, **record})
    return records


def normalized_rows(path, check):
    try:
        with safe_path(path).open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != TOTAL_FIELDS:
                raise AssetError("ASSET_SCHEMA_INVALID")
            seen = set()
            for number, row in enumerate(reader, 1):
                check()
                if number > MAX_ROWS:
                    raise AssetError("ASSET_ROW_LIMIT")
                if set(row) != set(TOTAL_FIELDS) or any(
                    v is None or len(v) > 4096 for v in row.values()
                ):
                    raise AssetError("ASSET_FIELD_INVALID")
                cohort, sha = key(row)
                if (cohort, sha) in seen:
                    raise AssetError("ASSET_DUPLICATE_SAMPLE")
                seen.add((cohort, sha))
                values = {k: float(row[k]) for k in NUMERIC_FIELDS}
                if any(not math.isfinite(v) or v < 0 for v in values.values()):
                    raise AssetError("ASSET_NUMERIC_INVALID")
                repository = (
                    "apache/hadoop"
                    if cohort in {"apache/hadoop", "apache/hadoop-hdfs", "apache/hadoop-mapreduce"}
                    else cohort
                )
                yield {
                    "row_number": number,
                    "cohort": cohort,
                    "canonical_repository": repository,
                    "commit_id": sha,
                    "sample_granularity": "commit",
                    "original_fields": row,
                    "event_epoch": epoch(row["author_date"]),
                    "event_clock": "published_author_epoch_UTC",
                    "published_label": "buggy" if boolean(row["buggy"]) else "clean",
                    "label_confidence": "published_SZZ_weak_supervision",
                    "label_available_at": None,
                    "numeric12": values,
                    "published_fix_metadata": boolean(row["fix"]),
                    "defect_types": None,
                    "type_status": "unknown",
                    "location": None,
                    "languages": [],
                    "language_status": "per_row_unverified",
                    "semantic_available": False,
                }
            if not seen:
                raise AssetError("ASSET_EMPTY")
    except (UnicodeError, csv.Error, ValueError) as exc:
        if isinstance(exc, AssetError):
            raise
        raise AssetError("ASSET_ROW_INVALID") from exc


def import_asset(published, store, *, verify_source, tick=None, seconds=MAX_SECONDS):
    """The explicit pinned-source verifier is supplied by the local ApacheJIT CLI."""
    published, store = safe_path(published), safe_path(store)
    if store.is_relative_to(published) or published.is_relative_to(store):
        raise AssetError("ASSET_INPUT_OUTPUT_COLLISION")
    budget = Budget(seconds=seconds, tick=tick)
    budget.check()
    acquisition = verify_source(published)
    utc_time(acquisition["acquired_at"])
    before = source_inventory(published, budget.check)
    audit = public_dataset.audit_paths(
        published / "apachejit_total.csv", published / "apachejit_date.csv"
    )
    budget.check()  # Existing audit has its own bounded 60-second cooperative supervision.
    rule = content_hash(
        {
            p.name: p.read_text(encoding="utf-8")
            for p in (Path(__file__), Path(public_dataset.__file__))
        }
    )
    store.mkdir(parents=True, exist_ok=True)
    attempt = safe_path(store / (".staging-" + uuid.uuid4().hex))
    attempt.mkdir()
    try:
        (attempt / "source").mkdir()
        for record in before:
            source = published / Path(record["path"]).name
            target = attempt / record["path"]
            with source.open("rb") as src, target.open("xb") as dst:
                size = 0
                while chunk := src.read(1024 * 1024):
                    budget.check()
                    size += len(chunk)
                    if size > record["bytes"]:
                        raise AssetError("ASSET_SOURCE_CHANGED")
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if file_digest(target, budget.check) != {k: record[k] for k in ("bytes", "sha256")}:
                raise AssetError("ASSET_SOURCE_CHANGED")
        labels, count, size = Counter(), 0, sum(r["bytes"] for r in before)
        with (attempt / "normalized.jsonl").open("xb") as dst:
            for row in normalized_rows(attempt / "source/apachejit_total.csv", budget.check):
                raw = canonical(row) + b"\n"
                size += len(raw)
                if len(raw) > 16384 or size > MAX_BYTES:
                    raise AssetError("ASSET_BYTE_LIMIT")
                dst.write(raw)
                labels[row["published_label"]] += 1
                count += 1
            dst.flush()
            os.fsync(dst.fileno())
        if count != audit["rows"] or dict(labels) != audit["labels"]:
            raise AssetError("ASSET_DERIVATION_CONFLICT")
        quality = sealed(
            {
                "version": VERSION,
                "rows": count,
                "labels": dict(labels),
                "public_audit": audit,
                "numeric_features": list(NUMERIC_FIELDS),
                "fix_is_metadata_not_product_FIX": True,
                "LT_synthesized": False,
                "capabilities": CAPABILITIES,
                "research_input_eligible_cohorts": audit["structurally_eligible_projects"],
                "language_scope": "Java_dominated_source_not_verified_per_row",
                "unknown_types": count,
                "missing_semantic_rows": count,
                "negative_coverage": "unknown",
                "gold_labels_certified": False,
            }
        )
        write_json(attempt / "quality.json", quality)
        inventory = [
            *before,
            *(
                {"path": n, **file_digest(attempt / n, budget.check)}
                for n in ("normalized.jsonl", "quality.json")
            ),
        ]
        if sum(r["bytes"] for r in inventory) > MAX_BYTES:
            raise AssetError("ASSET_BYTE_LIMIT")
        if (
            before != source_inventory(published, budget.check)
            or verify_source(published) != acquisition
        ):
            raise AssetError("ASSET_SOURCE_CHANGED")
        identity = {
            "adapter_version": VERSION,
            "adapter_rule_hash": rule,
            "source_acquisition": acquisition,
            "files": inventory,
        }
        asset_id = content_hash(identity)
        manifest = sealed(
            {
                "asset_id": asset_id,
                "identity": identity,
                "first_stored_at": datetime.now(UTC).isoformat(),
            }
        )
        write_json(attempt / "manifest.json", manifest)
        target = safe_path(store / asset_id)
        budget.check()
        if target.exists():
            return load_asset(target, tick=budget.check, seconds=seconds)
        try:
            os.rename(attempt, target)
        except OSError:
            if target.exists():
                return load_asset(target, tick=budget.check, seconds=seconds)
            raise
        return load_asset(target, tick=budget.check, seconds=seconds)
    finally:
        if attempt.exists():
            safe_path(attempt)
            shutil.rmtree(attempt)  # Only the UUID directory owned by this attempt, under runtime.


def load_asset(directory, *, tick=None, seconds=MAX_SECONDS):
    directory = safe_path(directory)
    budget = Budget(seconds=seconds, tick=tick)
    manifest = read_json(directory / "manifest.json", budget.check, maximum=128 * 1024)
    verify_seal(manifest)
    if set(manifest) != {"asset_id", "identity", "first_stored_at", "content_hash"}:
        raise AssetError("ASSET_MANIFEST_INVALID")
    identity = manifest["identity"]
    if not isinstance(identity, dict) or set(identity) != {
        "adapter_version",
        "adapter_rule_hash",
        "source_acquisition",
        "files",
    }:
        raise AssetError("ASSET_MANIFEST_INVALID")
    if identity["adapter_version"] != VERSION or not re.fullmatch(
        "[0-9a-f]{64}", str(identity["adapter_rule_hash"])
    ):
        raise AssetError("ASSET_VERSION_INVALID")
    if content_hash(identity) != manifest["asset_id"] or directory.name != manifest["asset_id"]:
        raise AssetError("ASSET_ID_INVALID")
    stored = datetime.fromisoformat(utc_time(manifest["first_stored_at"]))
    acquired = datetime.fromisoformat(utc_time(identity["source_acquisition"]["acquired_at"]))
    if stored < acquired:
        raise AssetError("ASSET_TIME_CONFLICT")
    expected_names = {"source/" + n for n in SOURCE_NAMES} | {"normalized.jsonl", "quality.json"}
    records = identity["files"]
    if (
        not isinstance(records, list)
        or len(records) != len(expected_names)
        or {r.get("path") for r in records if isinstance(r, dict)} != expected_names
    ):
        raise AssetError("ASSET_INVENTORY_INVALID")
    total = 0
    for record in records:
        if (
            set(record) != {"path", "bytes", "sha256"}
            or type(record["bytes"]) is not int
            or record["bytes"] < 0
            or not re.fullmatch("[0-9a-f]{64}", str(record["sha256"]))
        ):
            raise AssetError("ASSET_INVENTORY_INVALID")
        total += record["bytes"]
        if total > MAX_BYTES or file_digest(directory / record["path"], budget.check) != {
            k: record[k] for k in ("bytes", "sha256")
        }:
            raise AssetError("ASSET_FILE_DRIFT")
    actual = set()
    for parent in (directory, safe_path(directory / "source")):
        for p in parent.iterdir():
            safe_path(p)
            if p == directory / "source":
                continue
            if not p.is_file():
                raise AssetError("ASSET_INVENTORY_INVALID")
            actual.add(p.relative_to(directory).as_posix())
    if actual != expected_names | {"manifest.json"}:
        raise AssetError("ASSET_INVENTORY_INVALID")
    quality = read_json(directory / "quality.json", budget.check)
    verify_seal(quality)
    if (
        quality.get("capabilities") != CAPABILITIES
        or type(quality.get("rows")) is not int
        or not 1 <= quality["rows"] <= MAX_ROWS
    ):
        raise AssetError("ASSET_QUALITY_INVALID")
    if (
        file_digest(directory / "manifest.json", budget.check, maximum=128 * 1024)["sha256"]
        != hashlib.sha256(canonical(manifest) + b"\n").hexdigest()
    ):
        raise AssetError("ASSET_MANIFEST_CHANGED")
    return {"manifest": manifest, "quality": quality}


def page_asset(directory, *, offset=0, limit=100, tick=None):
    if (
        type(offset) is not int
        or not 0 <= offset <= MAX_ROWS
        or type(limit) is not int
        or not 1 <= limit <= 1000
    ):
        raise AssetError("ASSET_PAGE_INVALID")
    budget = Budget(tick=tick)
    result = load_asset(directory, tick=budget.check)
    rows = []
    with safe_path(Path(directory) / "normalized.jsonl").open("rb") as stream:
        for i, raw in enumerate(stream):
            budget.check()
            if len(raw) > 16384:
                raise AssetError("ASSET_FIELD_INVALID")
            if i >= offset:
                rows.append(json.loads(raw))
            if len(rows) == limit:
                break
    record = next(
        r for r in result["manifest"]["identity"]["files"] if r["path"] == "normalized.jsonl"
    )
    if file_digest(Path(directory) / "normalized.jsonl", budget.check) != {
        k: record[k] for k in ("bytes", "sha256")
    }:
        raise AssetError("ASSET_FILE_DRIFT")
    return {
        "asset_id": result["manifest"]["asset_id"],
        "total": result["quality"]["rows"],
        "offset": offset,
        "items": rows,
    }


def catalog(store, *, limit=100, tick=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AssetError("ASSET_PAGE_INVALID")
    store = safe_path(store)
    if not store.is_dir():
        raise AssetError("ASSET_STORE_INVALID")
    budget = Budget(tick=tick)
    entries = []
    for p in store.iterdir():
        budget.check()
        safe_path(p)
        if p.name.startswith(".staging-"):
            continue
        if not re.fullmatch("[0-9a-f]{64}", p.name):
            raise AssetError("ASSET_STORE_INVALID")
        entries.append(p)
        if len(entries) > limit:
            raise AssetError("ASSET_CATALOG_LIMIT")
    return [load_asset(p, tick=budget.check) for p in sorted(entries)]
