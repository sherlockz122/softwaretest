"""Audit frozen published members without recomputing today's dataset policy."""

import csv
import hashlib
import math
from contextlib import ExitStack
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.source_evidence import canonical, content_hash
from packages.modeling.policy import PURPOSE, ModelError
from packages.persistence.models import AsyncTask, DatasetMember, DatasetVersion


def reference(db, dataset_id):
    row = db.get(DatasetVersion, dataset_id)
    task = db.get(AsyncTask, row.latest_task_id) if row else None
    if not row:
        raise ModelError("MODEL_DATASET_NOT_FOUND", 404)
    if not row.output_json or not task or task.status != "succeeded":
        raise ModelError("MODEL_DATASET_NOT_READY", 409)
    output = row.output_json
    assets.verify_seal(output)
    metadata = output["metadata"]
    if (
        row.purpose != PURPOSE
        or metadata["policy"]["purpose"] != PURPOSE
        or metadata["features"] != list(research.NUMERIC_FIELDS)
        or not metadata["capabilities"].get("research_binary")
        or row.processed != row.total
        or row.total != metadata["rows"]
        or content_hash(row.plan_json) != row.plan_hash
        or row.plan_json != metadata
        or row.source_hash != metadata["source_hash"]
        or row.asset_id != metadata["asset_id"]
        or row.policy_json != metadata["policy"]
    ):
        raise ModelError("MODEL_DATASET_INCOMPATIBLE", 409)
    return {"dataset_id": row.id, "plan_hash": row.plan_hash, "manifest": output}


def audit_dataset(engine, frozen, destination, check, *, source_root=None):
    output = frozen["manifest"]
    directory = assets.safe_path(
        (source_root or research.OUTPUT)
        / str(UUID(frozen["dataset_id"]))
        / str(UUID(output["attempt_id"]))
    )
    if assets.read_json(directory / "manifest.json", check) != output:
        raise ModelError("MODEL_DATA_DRIFT", 409)
    for p in research.PARTITIONS:
        check()
        source = assets.safe_path(directory / (p + ".csv"))
        if assets.file_digest(source, check) != output["files"][p]:
            raise ModelError("MODEL_DATA_DRIFT", 409)
        if destination is not None:
            target = assets.safe_path(destination / (p + ".csv"))
            with source.open("rb") as read, target.open("xb") as write:
                while block := read.read(1024**2):
                    check()
                    write.write(block)
                write.flush()
                import os

                os.fsync(write.fileno())
            if assets.file_digest(target, check) != output["files"][p]:
                raise ModelError("MODEL_DATA_DRIFT", 409)
    digest, count = hashlib.sha256(b"["), 0
    counts = {p: [0, 0] for p in research.PARTITIONS}
    with ExitStack() as stack, Session(engine) as db:
        if reference(db, frozen["dataset_id"]) != frozen:
            raise ModelError("MODEL_DATA_DRIFT", 409)
        if (
            db.scalar(
                select(func.count())
                .select_from(DatasetMember)
                .where(DatasetMember.dataset_id == frozen["dataset_id"])
            )
            != output["metadata"]["rows"]
        ):
            raise ModelError("MODEL_DATA_DRIFT", 409)
        readers = {
            p: csv.DictReader(
                stack.enter_context((directory / (p + ".csv")).open(encoding="utf-8", newline=""))
            )
            for p in research.PARTITIONS
        }
        if any(tuple(r.fieldnames or ()) != research.CSV_FIELDS for r in readers.values()):
            raise ModelError("MODEL_DATA_DRIFT", 409)
        for offset in range(0, output["metadata"]["rows"], 500):
            check()
            rows = db.scalars(
                select(DatasetMember)
                .where(
                    DatasetMember.dataset_id == frozen["dataset_id"],
                    DatasetMember.ordinal >= offset,
                    DatasetMember.ordinal < offset + 500,
                )
                .order_by(DatasetMember.ordinal)
            ).all()
            for row in rows:
                if row.ordinal != count or row.partition not in readers:
                    raise ModelError("MODEL_DATA_DRIFT", 409)
                exported = next(readers[row.partition], None)
                if exported is None or set(exported) != set(research.CSV_FIELDS):
                    raise ModelError("MODEL_DATA_DRIFT", 409)
                values = {k: float(exported[k]) for k in research.NUMERIC_FIELDS}
                if (
                    exported["canonical_repository"] != row.canonical_repository
                    or exported["commit_id"] != row.commit_id
                    or int(exported["label"]) != row.label
                    or int(exported["event_epoch"]) != row.event_epoch
                    or any(not math.isfinite(v) or v < 0 for v in values.values())
                    or canonical(values) != canonical(row.numeric_json)
                ):
                    raise ModelError("MODEL_DATA_DRIFT", 409)
                member = {
                    "ordinal": count,
                    "partition": row.partition,
                    "canonical_repository": row.canonical_repository,
                    "commit_id": row.commit_id,
                    "event_epoch": row.event_epoch,
                    "label": row.label,
                    "numeric12": row.numeric_json,
                    "source_rows": row.source_json["row_numbers"],
                    "cohorts": row.source_json["cohorts"],
                }
                digest.update((b"," if count else b"") + canonical(member))
                counts[row.partition][row.label] += 1
                count += 1
        digest.update(b"]")
        if (
            count != output["metadata"]["rows"]
            or digest.hexdigest() != output["metadata"]["members_hash"]
        ):
            raise ModelError("MODEL_DATA_DRIFT", 409)
        if any(next(r, None) is not None for r in readers.values()):
            raise ModelError("MODEL_DATA_DRIFT", 409)
        if {p: {"clean": n[0], "buggy": n[1]} for p, n in counts.items()} != output["metadata"][
            "partitions"
        ]:
            raise ModelError("MODEL_DATA_DRIFT", 409)
    check()
