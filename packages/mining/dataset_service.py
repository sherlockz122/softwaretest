"""Application research datasets: fenced batches, recoverable roots, atomic readiness."""

import csv
import os
import shutil
import time
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.persistence.models import AsyncTask, DatasetMember, DatasetVersion
from packages.tasks.service import TaskError, TaskService, audit, database_now, key_valid

BATCH_SIZE = 500
MAX_BUILD_SECONDS = 900


class DatasetStopped(Exception):
    pass


class DatasetService:
    def __init__(self, settings, connections, *, store=None, output=None):
        self.settings, self.engine = settings, connections.engine
        self.tasks = TaskService(settings, connections)
        self.store, self.output = store or research.STORE, output or research.OUTPUT

    def create(self, user, key, request, request_id):
        key_valid(key)
        policy = {
            k: request[k]
            for k in ("purpose", "split_strategy", "train_ratio", "validation_ratio", "test_ratio")
        }
        research.policy_valid(policy)
        scope = user.id + ":dataset.build"

        def existing(db):
            row = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "dataset.build",
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            if row and row.payload["request"] != request:
                raise TaskError(409, "TASK_IDEMPOTENCY_CONFLICT")
            return (
                (self.tasks.acknowledgement(row) | {"dataset_id": row.root_task_id})
                if row
                else None
            )

        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := existing(db):
                    return replay
                # Only bounded metadata; heavyweight verification is performed by the Worker.
                source = research.preview(request["asset_id"], self.store)
                if source["source_hash"] != request["source_hash"]:
                    raise research.ResearchError("DATA_SOURCE_DRIFT")
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {
                        "dataset_version": research.VERSION,
                        "dataset_rule_hash": research.RULE_HASH,
                        "request": request,
                    },
                    scope,
                    request_id,
                    kind="dataset.build",
                )
                db.add(
                    DatasetVersion(
                        id=task.id,
                        latest_task_id=task.id,
                        name=request["name"],
                        purpose=request["purpose"],
                        asset_id=request["asset_id"],
                        source_hash=request["source_hash"],
                        policy_json=policy,
                    )
                )
                db.flush()
                return self.tasks.acknowledgement(task) | {"dataset_id": task.id}
        except IntegrityError as exc:
            if exc.orig.args[0] != 1062:
                raise
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := existing(db):
                    return replay
            raise

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "dataset.build"
            or task.execution_token != token
            or task.status not in {"running", "cancel_requested"}
            or task.lease_until is None
            or task.lease_until <= now
        ):
            return None
        if task.status == "cancel_requested":
            self.tasks.terminal(db, task, "cancelled")
            audit(db, task, "task.cancelled")
            return None
        dataset = db.get(DatasetVersion, task.root_task_id, with_for_update=True)
        if not dataset or dataset.latest_task_id != task.id or dataset.output_json is not None:
            return None
        if (
            task.payload.get("dataset_version") != research.VERSION
            or task.payload.get("dataset_rule_hash") != research.RULE_HASH
        ):
            self.tasks.terminal(db, task, "failed", "DATA_VERSION_CONFLICT")
            audit(db, task, "dataset.failed")
            return None
        return task, dataset, now

    def heartbeat(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, dataset, now = context
            task.processed, task.total = dataset.processed, dataset.total
            task.progress = min(99, 100 * dataset.processed / max(dataset.total or 0, 1))
            task.heartbeat_at = now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            return True

    def freeze(self, task_id, token, metadata, plan_hash):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            _, dataset, _ = context
            if (
                dataset.asset_id != metadata["asset_id"]
                or dataset.source_hash != metadata["source_hash"]
                or dataset.policy_json != metadata["policy"]
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            if dataset.plan_hash and (
                dataset.plan_hash != plan_hash or dataset.plan_json != metadata
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            dataset.plan_hash, dataset.plan_json, dataset.total = (
                plan_hash,
                metadata,
                metadata["rows"],
            )
            db.flush()
            return dataset.id, dataset.processed

    def batch(self, task_id, token, expected, members, plan_hash):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, dataset, now = context
            if (
                dataset.plan_hash != plan_hash
                or dataset.processed != expected
                or expected + len(members) > dataset.total
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            db.execute(
                insert(DatasetMember),
                [
                    {
                        "dataset_id": dataset.id,
                        "ordinal": row["ordinal"],
                        "canonical_repository": row["canonical_repository"],
                        "commit_id": row["commit_id"],
                        "event_epoch": row["event_epoch"],
                        "label": row["label"],
                        "partition": row["partition"],
                        "numeric_json": row["numeric12"],
                        "source_json": {
                            "row_numbers": row["source_rows"],
                            "cohorts": row["cohorts"],
                        },
                    }
                    for row in members
                ],
            )
            dataset.processed += len(members)
            task.processed, task.total = dataset.processed, dataset.total
            task.progress, task.stage = (
                min(99, 100 * dataset.processed / max(dataset.total, 1)),
                "building_dataset",
            )
            task.heartbeat_at, task.lease_until = (
                now,
                now + timedelta(seconds=self.settings.task_lease_seconds),
            )
            task.version += 1
            db.flush()
            return True

    def artifacts(self, dataset_id, token, members, metadata, check):
        directory = assets.safe_path(self.output / str(UUID(dataset_id)) / str(UUID(token)))
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        budget = assets.Budget(tick=check)
        records = {}
        for partition in research.PARTITIONS:
            path = assets.safe_path(directory / (partition + ".csv"))
            with path.open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=research.CSV_FIELDS)
                writer.writeheader()
                for member in members:
                    budget.check()
                    if member["partition"] == partition:
                        writer.writerow(
                            {k: member[k] for k in research.CSV_FIELDS[:4]} | member["numeric12"]
                        )
                stream.flush()
                os.fsync(stream.fileno())
            records[partition] = assets.file_digest(path, budget.check)
        output = assets.sealed(
            {"dataset_id": dataset_id, "attempt_id": token, "metadata": metadata, "files": records}
        )
        assets.write_json(directory / "manifest.json", output)
        budget.check()
        return output

    def verify_members(self, dataset_id, members, count, check):
        for offset in range(0, count, BATCH_SIZE):
            check()
            with Session(self.engine) as db:
                rows = db.scalars(
                    select(DatasetMember)
                    .where(
                        DatasetMember.dataset_id == dataset_id,
                        DatasetMember.ordinal >= offset,
                        DatasetMember.ordinal < min(offset + BATCH_SIZE, count),
                    )
                    .order_by(DatasetMember.ordinal)
                ).all()
                expected = members[offset : min(offset + BATCH_SIZE, count)]
                if len(rows) != len(expected):
                    raise research.ResearchError("DATA_MEMBER_DRIFT")
                for row, item in zip(rows, expected, strict=True):
                    if (
                        row.ordinal != item["ordinal"]
                        or row.canonical_repository != item["canonical_repository"]
                        or row.commit_id != item["commit_id"]
                        or row.partition != item["partition"]
                        or row.label != item["label"]
                        or row.event_epoch != item["event_epoch"]
                        or assets.canonical(row.numeric_json) != assets.canonical(item["numeric12"])
                        or row.source_json
                        != {"row_numbers": item["source_rows"], "cohorts": item["cohorts"]}
                    ):
                        raise research.ResearchError("DATA_MEMBER_DRIFT")

    def publish(self, task_id, token, plan_hash, output):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, dataset, _ = context
            if (
                dataset.plan_hash != plan_hash
                or dataset.processed != dataset.total
                or output["metadata"] != dataset.plan_json
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            if (
                db.scalar(
                    select(func.count())
                    .select_from(DatasetMember)
                    .where(DatasetMember.dataset_id == dataset.id)
                )
                != dataset.total
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            dataset.output_json = output
            task.result_json = {
                "dataset_id": dataset.id,
                "purpose": dataset.purpose,
                "rows": dataset.total,
                "members_hash": dataset.plan_json["members_hash"],
                "manifest_hash": output["content_hash"],
            }
            task.progress = 100
            self.tasks.terminal(db, task, "succeeded")
            audit(db, task, "dataset.publish")
            db.flush()
            return True

    def run(self, task_id, token, payload, *, after_batch=None):
        last = 0
        last_capacity = 0
        deadline = time.monotonic() + MAX_BUILD_SECONDS

        def check():
            nonlocal last, last_capacity
            if time.monotonic() > deadline:
                raise research.ResearchError("DATA_BUILD_DEADLINE")
            if time.monotonic() - last_capacity >= 2:
                destination = assets.safe_path(self.output)
                physical = destination if destination.exists() else destination.parent
                if shutil.disk_usage(physical).free < 2.5 * 1024**3:
                    raise research.ResearchError("DATA_CAPACITY_UNSAFE")
                last_capacity = time.monotonic()
            if time.monotonic() - last >= min(3, self.settings.task_heartbeat_seconds):
                if not self.heartbeat(task_id, token):
                    raise DatasetStopped
                last = time.monotonic()

        try:
            check()
            request = payload["request"]
            policy = {
                k: request[k]
                for k in (
                    "purpose",
                    "split_strategy",
                    "train_ratio",
                    "validation_ratio",
                    "test_ratio",
                )
            }
            members, metadata, plan_hash = research.prepare(
                research.asset_path(request["asset_id"], self.store),
                request["source_hash"],
                policy,
                tick=check,
            )
            frozen = self.freeze(task_id, token, metadata, plan_hash)
            if frozen is None:
                raise DatasetStopped
            dataset_id, processed = frozen
            self.verify_members(dataset_id, members, processed, check)
            for offset in range(processed, len(members), BATCH_SIZE):
                check()
                if not self.batch(
                    task_id, token, offset, members[offset : offset + BATCH_SIZE], plan_hash
                ):
                    raise DatasetStopped
                if after_batch:
                    after_batch(offset + len(members[offset : offset + BATCH_SIZE]))
            self.verify_members(dataset_id, members, len(members), check)
            output = self.artifacts(dataset_id, token, members, metadata, check)
            self.publish(task_id, token, plan_hash, output)
        except DatasetStopped:
            return
        except (
            research.ResearchError,
            assets.AssetError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            with Session(self.engine) as db, db.begin():
                context = self.fenced(db, task_id, token)
                if context:
                    task, _, _ = context
                    code = (
                        exc.code
                        if isinstance(exc, research.ResearchError)
                        else "DATA_SOURCE_OR_STORAGE_INVALID"
                    )
                    self.tasks.terminal(db, task, "failed", code)
                    audit(db, task, "dataset.failed")

    @staticmethod
    def visible(dataset, task):
        ready = dataset.output_json is not None and task.status == "succeeded"
        return {
            "id": dataset.id,
            "name": dataset.name,
            "purpose": dataset.purpose,
            "asset_id": dataset.asset_id,
            "source_hash": dataset.source_hash,
            "policy": dataset.policy_json,
            "created_at": dataset.created_at.isoformat() + "Z",
            "task_id": task.id,
            "status": task.status,
            "processed": dataset.processed,
            "total": dataset.total,
            "ready": ready,
            "summary": dataset.output_json["metadata"] if ready else None,
            "manifest_hash": dataset.output_json["content_hash"] if ready else None,
        }

    def listing(self, page=1, page_size=20):
        with Session(self.engine) as db:
            total = db.scalar(select(func.count()).select_from(DatasetVersion))
            rows = db.execute(
                select(DatasetVersion, AsyncTask)
                .join(AsyncTask, DatasetVersion.latest_task_id == AsyncTask.id)
                .order_by(DatasetVersion.created_at.desc(), DatasetVersion.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [self.visible(d, t) for d, t in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "assets": research.previews(self.store),
            }

    def detail(self, dataset_id, offset=0, limit=100):
        with Session(self.engine) as db:
            dataset = db.get(DatasetVersion, dataset_id)
            if not dataset:
                raise TaskError(404, "DATA_NOT_FOUND")
            task = db.get(AsyncTask, dataset.latest_task_id)
            result = self.visible(dataset, task)
            result["members"] = []
            if result["ready"]:
                rows = db.scalars(
                    select(DatasetMember)
                    .where(DatasetMember.dataset_id == dataset_id)
                    .order_by(DatasetMember.ordinal)
                    .offset(offset)
                    .limit(limit)
                )
                result["members"] = [
                    {
                        "ordinal": r.ordinal,
                        "canonical_repository": r.canonical_repository,
                        "commit_id": r.commit_id,
                        "partition": r.partition,
                        "label": r.label,
                        "event_epoch": r.event_epoch,
                        "numeric12": r.numeric_json,
                        "source": r.source_json,
                    }
                    for r in rows
                ]
                result["exports"] = dataset.output_json["files"]
            result.update(offset=offset, limit=limit)
            return result

    def download(self, dataset_id, partition):
        if partition not in research.PARTITIONS:
            raise TaskError(422, "DATA_POLICY_INVALID")
        with Session(self.engine) as db:
            dataset = db.get(DatasetVersion, dataset_id)
            if not dataset:
                raise TaskError(404, "DATA_NOT_FOUND")
            task = db.get(AsyncTask, dataset.latest_task_id)
            if not dataset.output_json or task.status != "succeeded":
                raise TaskError(409, "DATA_NOT_READY")
            output = dataset.output_json
        assets.verify_seal(output)
        directory = assets.safe_path(
            self.output / str(UUID(dataset_id)) / str(UUID(output["attempt_id"]))
        )
        budget = assets.Budget(seconds=30)
        stored = assets.read_json(directory / "manifest.json", budget.check)
        if stored != output:
            raise research.ResearchError("DATA_ARTIFACT_DRIFT")
        path = assets.safe_path(directory / (partition + ".csv"))
        if assets.file_digest(path, budget.check) != output["files"][partition]:
            raise research.ResearchError("DATA_ARTIFACT_DRIFT")
        return path
