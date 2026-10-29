"""Integrity-only durable tasks; all qualification flags remain unchanged."""

import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.mining.source_evidence import content_hash
from packages.persistence.models import AsyncTask
from packages.semantic import artifacts
from packages.semantic import asset_catalog as catalog
from packages.semantic import asset_reader as reader
from packages.tasks.service import TaskService, audit, database_now, key_valid

VERSION = "semantic-integrity-task-v1"
KIND = "semantic.verify"
SOURCES = (
    "packages/semantic/asset_reader.py",
    "packages/semantic/asset_catalog.py",
    "packages/semantic/asset_lock.py",
    "packages/semantic/verification.py",
    "packages/tasks/service.py",
    "packages/tasks/kinds.py",
    "apps/worker/tasks.py",
)


def consumer_hash():
    return content_hash(artifacts.sources(SOURCES))


class VerificationStopped(Exception):
    pass


class VerificationService:
    def __init__(self, settings, connections, *, store=None):
        self.settings, self.engine = settings, connections.engine
        self.tasks = TaskService(settings, connections)
        self.store = store or catalog.STORE

    def create(self, user, key, identity, request_id):
        key_valid(key)
        reader.require(reader.digest_id(identity), "SEMANTIC_ASSET_ID")
        scope = user.id + ":" + KIND

        def replay(db):
            row = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == KIND,
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            if row:
                reader.require(row.payload.get("asset_id") == identity, "TASK_IDEMPOTENCY_CONFLICT")
                return self.tasks.acknowledgement(row)

        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if result := replay(db):
                    return result
                path = reader.safe(self.store / identity)
                if not path.exists():
                    raise reader.ConsumptionError("SEMANTIC_ASSET_NOT_FOUND", 404)
                value = catalog._checked(lambda: catalog.receipt(path, reader.Budget()))[0]
                payload = {
                    "semantic_verification_version": VERSION,
                    "asset_id": identity,
                    "origin_artifact": value["origin_artifact"],
                    "rule_hash": catalog.RULE_HASH,
                    "consumer_hash": consumer_hash(),
                }
                task = self.tasks.new_task(db, user.id, key, payload, scope, request_id, kind=KIND)
                return self.tasks.acknowledgement(task)
        except IntegrityError as exc:
            if exc.orig.args[0] != 1062:
                raise
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if result := replay(db):
                    return result
            raise

    def live(self, db, task_id, token, payload):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != KIND
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
        if (
            not isinstance(payload, dict)
            or task.payload != payload
            or set(payload)
            != {
                "semantic_verification_version",
                "asset_id",
                "origin_artifact",
                "rule_hash",
                "consumer_hash",
            }
            or payload["semantic_verification_version"] != VERSION
            or payload["rule_hash"] != catalog.RULE_HASH
            or payload["consumer_hash"] != consumer_hash()
            or not reader.digest_id(payload["asset_id"])
            or not reader.digest_id(payload["origin_artifact"])
            or task.result_json is not None
        ):
            self.tasks.terminal(db, task, "failed", "SEMANTIC_TASK_BINDING_CONFLICT")
            audit(db, task, "semantic.verify.failed")
            return None
        return task, now

    def heartbeat(self, task_id, token, payload, stage="verifying_asset"):
        with Session(self.engine) as db, db.begin():
            context = self.live(db, task_id, token, payload)
            if not context:
                return False
            task, now = context
            task.stage, task.total, task.processed = stage, 660, 0
            task.progress = 90 if stage == "recording_verification" else 5
            task.heartbeat_at = now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            return True

    def failed(self, task_id, token, payload, code):
        with Session(self.engine) as db, db.begin():
            context = self.live(db, task_id, token, payload)
            if context:
                task, _ = context
                self.tasks.terminal(db, task, "failed", code)
                audit(db, task, "semantic.verify.failed")

    def publish(self, task_id, token, payload, value, *, deadline=None):
        with Session(self.engine) as db, db.begin():
            context = self.live(db, task_id, token, payload)
            if not context:
                return False
            task, now = context
            current = catalog._checked(
                lambda: catalog.receipt(self.store / payload["asset_id"], reader.Budget())
            )[0]
            reader.require(
                current == value
                and current["content_hash"] == payload["asset_id"]
                and current["origin_artifact"] == payload["origin_artifact"],
                "SEMANTIC_ASSET_DRIFT",
            )
            budget = reader.Budget()
            if deadline is not None:
                budget.deadline = deadline
            for name in sorted(catalog.FILES):
                maximum = 65536 if name == "origin-manifest.json" else reader.CAPS[name]
                reader.require(
                    reader.file_record(self.store / payload["asset_id"] / name, maximum, budget)
                    == value["files"][name],
                    "SEMANTIC_ASSET_DRIFT",
                )
            reader.require(
                catalog.receipt(self.store / payload["asset_id"], budget)[0] == value,
                "SEMANTIC_ASSET_DRIFT",
            )
            context = self.live(db, task_id, token, payload)
            if not context:
                return False
            task, now = context
            task.result_json = {
                "semantic_asset_id": payload["asset_id"],
                "version": VERSION,
                "asset_version": catalog.VERSION,
                "origin_artifact": payload["origin_artifact"],
                "rule_hash": payload["rule_hash"],
                "consumer_hash": payload["consumer_hash"],
                "shape": value["shape"],
                "partitions": value["partitions"],
                "payload_hashes": value["files"],
                "verified_at": now.isoformat(timespec="microseconds") + "Z",
                "capabilities": catalog.CAPABILITIES,
                "snapshot_only": True,
            }
            task.progress, task.processed, task.total = 100, 660, 660
            self.tasks.terminal(db, task, "succeeded")
            audit(db, task, "semantic.verify.completed")
            db.flush()
            return True

    def run(self, task_id, token, payload, *, observe=None):
        last = 0

        def check():
            nonlocal last
            now = time.monotonic()
            if now - last >= min(2, self.settings.task_heartbeat_seconds):
                if not self.heartbeat(task_id, token, payload):
                    raise VerificationStopped
                last = now
            if observe:
                observe()

        try:
            check()
            budget = reader.Budget(tick=check)
            value, _, _ = catalog.verify(self.store / payload["asset_id"], budget=budget)
            if not self.heartbeat(task_id, token, payload, "recording_verification"):
                return
            self.publish(task_id, token, payload, value, deadline=budget.deadline)
        except VerificationStopped:
            return
        except reader.ConsumptionError as exc:
            self.failed(task_id, token, payload, exc.code)
