"""Immutable restricted-research catalog and projections for application readers."""

import os
import shutil
from pathlib import Path
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import asset_reader as reader
from packages.semantic.asset_lock import publication

STORE = assets.ROOT / "runtime/semantic-assets"
VERSION = "semantic-asset-read-v1"
CAPABILITIES = {
    "structurally_readable": True,
    "training_ready": False,
    "historical_replay": False,
    "candidate_release": False,
    "type_prediction": False,
    "language_support_certified": False,
}
CONTRACT = {
    "version": VERSION,
    "producer": reader.PRODUCER,
    "rows": 660,
    "columns": 3076,
    "caps": reader.CAPS,
    "receipt_maximum": 32768,
    "catalog_maximum": 64,
}
RULE_HASH = content_hash(CONTRACT)
FILES = reader.PAYLOADS | {"origin-manifest.json"}


def _checked(action):
    try:
        return action()
    except reader.ConsumptionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise reader.ConsumptionError("SEMANTIC_ASSET_UNAVAILABLE") from exc


def receipt(directory, budget):
    directory = reader.safe(directory)
    value = reader.read_json(directory / "manifest.json", 32768, budget)
    reader.seal(value)
    reader.require(
        set(value)
        == {
            "version",
            "rule_hash",
            "origin_artifact",
            "accepted",
            "shape",
            "partitions",
            "capabilities",
            "files",
            "content_hash",
        }
    )
    reader.require(value["version"] == VERSION and value["rule_hash"] == RULE_HASH)
    reader.require(value["content_hash"] == directory.name and reader.digest_id(directory.name))
    reader.require(
        type(value["accepted"]) is int
        and value["accepted"] == 660
        and value["shape"] == [660, 3076]
    )
    reader.require(value["partitions"] == {"train": 488, "validation": 172})
    reader.require(value["capabilities"] == CAPABILITIES)
    reader.require(all(type(v) is bool for v in value["capabilities"].values()))
    reader.require(all(type(v) is int for v in value["partitions"].values()))
    reader.require(isinstance(value["files"], dict) and set(value["files"]) == FILES)
    for name, record in value["files"].items():
        reader.descriptor(
            record,
            reader.CAPS["manifest.json"] if name == "origin-manifest.json" else reader.CAPS[name],
        )
    original = reader.origin(
        directory, value["origin_artifact"], budget, filename="origin-manifest.json"
    )
    reader.require(
        original["files"] == {k: v for k, v in value["files"].items() if k in reader.PAYLOADS}
    )
    reader.require(
        reader.file_record(directory / "origin-manifest.json", 65536, budget)
        == value["files"]["origin-manifest.json"],
        "SEMANTIC_ASSET_DRIFT",
    )
    return value, original


def verify(directory, *, budget=None):
    budget = budget or reader.Budget()

    def work():
        value, original = receipt(directory, budget)
        rows, coverage = reader.payload(Path(directory), original, budget)
        reader.require(receipt(directory, budget) == (value, original), "SEMANTIC_ASSET_DRIFT")
        # Re-hash JSON after numeric validation to reject concurrent replacement.
        for name in sorted(reader.PAYLOADS):
            reader.require(
                reader.file_record(Path(directory) / name, reader.CAPS[name], budget)
                == value["files"][name],
                "SEMANTIC_ASSET_DRIFT",
            )
        reader.require(receipt(directory, budget) == (value, original), "SEMANTIC_ASSET_DRIFT")
        return value, rows, coverage

    return _checked(work)


def _copy(source, target, expected, maximum, budget):
    reader.descriptor(expected, maximum)
    reader.require(reader.file_record(source, maximum, budget) == expected, "SEMANTIC_ASSET_DRIFT")
    before = reader.stamp(source)
    count = 0
    with reader.safe(source).open("rb") as incoming, reader.safe(target).open("xb") as outgoing:
        while raw := incoming.read(65536):
            budget.check()
            count += len(raw)
            reader.require(count <= expected["bytes"], "SEMANTIC_ASSET_DRIFT")
            if shutil.disk_usage(assets.ROOT).free < 2 * 1024**3:
                raise reader.ConsumptionError("SEMANTIC_ASSET_CAPACITY")
            outgoing.write(raw)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    reader.require(
        count == expected["bytes"] and reader.stamp(source) == before, "SEMANTIC_ASSET_DRIFT"
    )
    reader.require(reader.file_record(target, maximum, budget) == expected, "SEMANTIC_ASSET_DRIFT")


def install(source, expected, *, store=None, budget=None):
    """Trusted local deployment operation; never accepts HTTP-supplied paths."""
    budget = budget or reader.Budget()
    store = reader.safe(store or STORE)
    source = reader.safe(source)

    def work():
        reader.require(not source.is_relative_to(store), "SEMANTIC_ASSET_SOURCE")
        original = reader.origin(source, expected, budget)
        # Historical producer directories hash the entire sealed manifest;
        # content_hash inside that manifest hashes the payload without its seal.
        reader.require(source.name == content_hash(original), "SEMANTIC_ASSET_SOURCE")
        reader.payload(source, original, budget)
        original_file = reader.file_record(source / "manifest.json", 65536, budget)
        data = assets.sealed(
            {
                "version": VERSION,
                "rule_hash": RULE_HASH,
                "origin_artifact": expected,
                "accepted": 660,
                "shape": [660, 3076],
                "partitions": {"train": 488, "validation": 172},
                "capabilities": CAPABILITIES,
                "files": original["files"] | {"origin-manifest.json": original_file},
            }
        )
        store.mkdir(parents=True, exist_ok=True)
        target = reader.safe(store / data["content_hash"])
        if target.exists():
            reader.require(verify(target, budget=budget)[0] == data, "SEMANTIC_ASSET_CONFLICT")
            return target
        entries = catalog_ids(store)
        reader.require(len(entries) < 64, "SEMANTIC_ASSET_CATALOG_LIMIT")
        attempt = reader.safe(store / (".install-" + uuid4().hex))
        attempt.mkdir()
        for name in sorted(FILES):
            _copy(
                source / ("manifest.json" if name == "origin-manifest.json" else name),
                attempt / name,
                data["files"][name],
                65536 if name == "origin-manifest.json" else reader.CAPS[name],
                budget,
            )
        assets.write_json(attempt / "manifest.json", data)
        # Verify using its final identity without renaming/publishing the attempt.
        reader.require(reader.origin(source, expected, budget) == original, "SEMANTIC_ASSET_DRIFT")
        for name in reader.PAYLOADS:
            reader.require(
                reader.file_record(source / name, reader.CAPS[name], budget)
                == original["files"][name],
                "SEMANTIC_ASSET_DRIFT",
            )
        checked = reader.read_json(attempt / "manifest.json", 32768, budget)
        reader.seal(checked)
        reader.require(checked == data)
        reader.require(
            reader.origin(attempt, expected, budget, filename="origin-manifest.json") == original
        )
        reader.payload(attempt, original, budget)
        reader.require(reader.origin(source, expected, budget) == original, "SEMANTIC_ASSET_DRIFT")
        for name in sorted(reader.PAYLOADS):
            reader.require(
                reader.file_record(source / name, reader.CAPS[name], budget)
                == original["files"][name],
                "SEMANTIC_ASSET_DRIFT",
            )
        budget.check()
        with publication(store):
            budget.check()
            if target.exists():
                reader.require(verify(target, budget=budget)[0] == data, "SEMANTIC_ASSET_CONFLICT")
            else:
                reader.require(len(catalog_ids(store)) < 64, "SEMANTIC_ASSET_CATALOG_LIMIT")
                attempt.rename(target)
        return target

    return _checked(work)


def catalog_ids(store=None):
    store = reader.safe(store or STORE)
    if not store.exists():
        return []
    ids = []
    with os.scandir(store) as entries:
        inspected = 0
        for entry in entries:
            inspected += 1
            reader.require(inspected <= 256, "SEMANTIC_ASSET_CATALOG_LIMIT")
            if entry.name.startswith("."):
                continue
            reader.require(reader.digest_id(entry.name), "SEMANTIC_ASSET_CATALOG_INVALID")
            path = reader.safe(store / entry.name)
            reader.require(path.is_dir(), "SEMANTIC_ASSET_CATALOG_INVALID")
            ids.append(entry.name)
            reader.require(len(ids) <= 64, "SEMANTIC_ASSET_CATALOG_LIMIT")
    return sorted(ids)


def summary(value, original):
    return {
        "id": value["content_hash"],
        "version": VERSION,
        "producer_version": original["version"],
        "origin_artifact": value["origin_artifact"],
        "input_hash": original["binding"]["inputs"],
        "checkpoint_hash": original["binding"]["checkpoint"],
        "accepted": 660,
        "shape": [660, 3076],
        "partitions": value["partitions"],
        "capabilities": value["capabilities"],
        "limitations": [
            "来源、许可和独立专业复核未认证",
            "完整变更覆盖、模型效用及独立评价未认证",
            "不用于历史、候选预测或产品发布",
        ],
    }


def listing(page=1, page_size=20, *, store=None):
    reader.require(
        type(page) is int and page >= 1 and type(page_size) is int and 1 <= page_size <= 100,
        "SEMANTIC_ASSET_PAGING",
    )
    store = reader.safe(store or STORE)
    budget = reader.Budget()

    def work():
        ids = catalog_ids(store)
        selected = ids[(page - 1) * page_size : page * page_size]
        items = [
            summary(*receipt(store / identity, budget)) | {"payload_verified": False}
            for identity in selected
        ]
        reader.require(catalog_ids(store) == ids, "SEMANTIC_ASSET_CATALOG_DRIFT")
        return {"items": items, "total": len(ids), "page": page, "page_size": page_size}

    return _checked(work)


def detail(identity, offset=0, limit=100, partition=None, *, store=None):
    reader.require(reader.digest_id(identity), "SEMANTIC_ASSET_ID")
    reader.require(
        type(offset) is int
        and 0 <= offset <= 660
        and type(limit) is int
        and 1 <= limit <= 100
        and partition in (None, "train", "validation"),
        "SEMANTIC_ASSET_PAGING",
    )
    store = reader.safe(store or STORE)
    path = reader.safe(store / identity)
    if not path.exists():
        raise reader.ConsumptionError("SEMANTIC_ASSET_NOT_FOUND", 404)

    def work():
        budget = reader.Budget()
        value, rows, coverage = verify(path, budget=budget)
        original = reader.origin(
            path, value["origin_artifact"], budget, filename="origin-manifest.json"
        )
        selected = [
            row | {"coverage": audit["roles"]}
            for row, audit in zip(rows, coverage, strict=True)
            if partition is None or row["member"]["partition"] == partition
        ]
        return summary(value, original) | {
            "payload_verified": True,
            "items": selected[offset : offset + limit],
            "total": len(selected),
            "offset": offset,
            "limit": limit,
            "partition": partition,
        }

    return _checked(work)
