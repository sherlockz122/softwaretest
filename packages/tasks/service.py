"""Database is the authority for requests, execution leases and task outcomes."""

import hashlib
import json
import re
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.auth.security import AuthError, CurrentUser, authorize_task_owner, require_role
from packages.persistence.models import AsyncTask, OperationLog, TaskOutbox, User

STATES = {"queued", "running", "cancel_requested", "succeeded", "failed", "cancelled"}


class TaskError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def key_valid(key):
    if not key or not re.fullmatch(r"[\x21-\x7e]{1,128}", key):
        raise TaskError(422, "TASK_INVALID_INPUT")
    return key


def database_now(db):
    return db.scalar(select(func.utc_timestamp(6)))


def payload_hash(scope, payload):
    canonical = json.dumps(
        {"version": 1, "type": "diagnostic", "scope": scope, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def audit(db, task, action, actor_id=None):
    db.add(
        OperationLog(
            actor_id=actor_id or task.actor_id,
            action=action,
            object_type="async_task",
            object_id=task.id,
            result="succeeded",
            request_id=task.request_id,
            detail_json={},
        )
    )


def public(task, timestamp):
    def iso(value):
        return value.isoformat(timespec="microseconds") + "Z" if value else None

    return {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "stage": task.stage,
        "progress": float(task.progress),
        "processed": task.processed,
        "total": task.total,
        "heartbeat_at": iso(task.heartbeat_at),
        "created_at": iso(task.created_at),
        "started_at": iso(task.started_at),
        "finished_at": iso(task.finished_at),
        "health": "stale"
        if task.status in {"running", "cancel_requested"}
        and (task.lease_until is None or task.lease_until <= timestamp)
        else "healthy",
        "retry_of": task.retry_of,
        "result": task.result_json,
        "error": {
            "code": task.error_code,
            "message": task.error_message,
            "request_id": task.request_id,
        }
        if task.error_code
        else None,
    }


class TaskService:
    def __init__(self, settings, connections):
        self.settings, self.engine = settings, connections.engine

    def actor(self, db, user):
        # Shared user lock keeps authorization current without serializing all creators.
        row = db.scalar(select(User).where(User.id == user.id).with_for_update(read=True))
        if not row or not row.is_active:
            raise AuthError(401, "AUTH_REQUIRED")
        return require_role(CurrentUser(row.id, row.username, row.role), "Member")

    def new_task(self, db, actor_id, key, payload, scope, request_id, parent=None):
        task_id = str(uuid4())
        canonical = json.dumps(
            {"version": 1, "type": "diagnostic", "scope": scope, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        task = AsyncTask(
            id=task_id,
            actor_id=actor_id,
            type="diagnostic",
            scope_key=scope,
            idempotency_key=key,
            payload_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            payload=payload,
            root_task_id=parent.root_task_id if parent else task_id,
            retry_of=parent.id if parent else None,
            retry_count=parent.retry_count + 1 if parent else 0,
            queued_deadline=database_now(db) + timedelta(seconds=self.settings.task_queue_seconds),
            request_id=request_id,
            total=payload["duration_seconds"],
        )
        db.add(task)
        db.flush()
        db.add(TaskOutbox(task_id=task.id, next_attempt_at=database_now(db)))
        audit(db, task, "task.retry" if parent else "task.create")
        return task

    @staticmethod
    def acknowledgement(task):
        result = {"task_id": task.id, "status": task.status}
        if task.retry_of:
            result["retry_of"] = task.retry_of
        return result

    def create(self, user, key, payload, request_id):
        key_valid(key)
        scope = user.id + ":diagnostic"
        try:
            with Session(self.engine) as db, db.begin():
                self.actor(db, user)
                existing = db.scalar(
                    select(AsyncTask).where(
                        AsyncTask.type == "diagnostic",
                        AsyncTask.scope_key == scope,
                        AsyncTask.idempotency_key == key,
                    )
                )
                if existing:
                    if existing.payload_hash != payload_hash(scope, payload):
                        raise TaskError(409, "TASK_IDEMPOTENCY_CONFLICT")
                    return self.acknowledgement(existing)
                return self.acknowledgement(
                    self.new_task(db, user.id, key, payload, scope, request_id)
                )
        except IntegrityError as exc:
            if exc.orig.args[0] != 1062:
                raise
            # A concurrent insert committed first. Read in a fresh transaction/snapshot.
            with Session(self.engine) as db:
                existing = db.scalar(
                    select(AsyncTask).where(
                        AsyncTask.type == "diagnostic",
                        AsyncTask.scope_key == scope,
                        AsyncTask.idempotency_key == key,
                    )
                )
                if not existing:
                    raise
                if existing.payload_hash != payload_hash(scope, payload):
                    raise TaskError(409, "TASK_IDEMPOTENCY_CONFLICT") from None
                return self.acknowledgement(existing)

    @staticmethod
    def locked(db, task_id):
        task = db.scalar(select(AsyncTask).where(AsyncTask.id == task_id).with_for_update())
        if task is None:
            raise TaskError(404, "TASK_NOT_FOUND")
        return task

    def detail(self, task_id):
        with Session(self.engine) as db:
            task = db.get(AsyncTask, task_id)
            if task is None:
                raise TaskError(404, "TASK_NOT_FOUND")
            return public(task, database_now(db))

    def listing(self, kind=None, status=None, page=1, page_size=20):
        if kind not in {None, "diagnostic"} or status not in STATES | {None}:
            raise TaskError(422, "TASK_INVALID_INPUT")
        with Session(self.engine) as db, db.begin():
            conditions = []
            if kind:
                conditions.append(AsyncTask.type == kind)
            if status:
                conditions.append(AsyncTask.status == status)
            total = db.scalar(select(func.count()).select_from(AsyncTask).where(*conditions))
            tasks = db.scalars(
                select(AsyncTask)
                .where(*conditions)
                .order_by(AsyncTask.created_at.desc(), AsyncTask.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            timestamp = database_now(db)
            return {
                "items": [public(task, timestamp) for task in tasks],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def cancel(self, user, task_id, request_id):
        with Session(self.engine) as db, db.begin():
            actor = self.actor(db, user)
            task = self.locked(db, task_id)
            authorize_task_owner(actor, task.actor_id)
            if task.status == "cancelled":
                return 204, None
            if task.status in {"succeeded", "failed"}:
                raise TaskError(409, "TASK_STATE_CONFLICT")
            if task.status == "queued":
                self.terminal(db, task, "cancelled")
                task.request_id = request_id
                audit(db, task, "task.cancel", user.id)
                return 204, None
            if task.status == "running":
                task.status, task.stage = "cancel_requested", "cancelling"
                task.version += 1
                task.request_id = request_id
                audit(db, task, "task.cancel_requested", user.id)
            return 202, self.acknowledgement(task)

    def retry(self, user, task_id, key, request_id):
        key_valid(key)
        with Session(self.engine) as db, db.begin():
            actor = self.actor(db, user)
            parent = self.locked(db, task_id)
            authorize_task_owner(actor, parent.actor_id)
            if parent.status not in {"failed", "cancelled"}:
                raise TaskError(409, "TASK_STATE_CONFLICT")
            existing = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == parent.id))
            scope = user.id + ":diagnostic:retry:" + parent.id
            if existing:
                if existing.scope_key == scope and existing.idempotency_key == key:
                    return self.acknowledgement(existing)
                raise TaskError(409, "TASK_RETRY_EXISTS")
            return self.acknowledgement(
                self.new_task(db, user.id, key, parent.payload, scope, request_id, parent)
            )

    @staticmethod
    def terminal(db, task, status, code=None):
        task.status, task.stage = status, status
        task.finished_at = database_now(db)
        task.lease_until = None
        task.version += 1
        task.error_code = code
        task.error_message = "任务未完成，请根据任务编号检查或重试" if code else None

    def claim(self, task_id):
        with Session(self.engine) as db, db.begin():
            task = db.scalar(select(AsyncTask).where(AsyncTask.id == task_id).with_for_update())
            timestamp = database_now(db)
            if not task or task.status != "queued" or task.queued_deadline <= timestamp:
                return None
            if task.type != "diagnostic":
                self.terminal(db, task, "failed", "TASK_UNKNOWN_TYPE")
                audit(db, task, "task.failed")
                return None
            task.status, task.stage = "running", "diagnostic"
            task.started_at = task.heartbeat_at = timestamp
            task.execution_token = str(uuid4())
            task.lease_until = timestamp + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            audit(db, task, "task.start")
            return task.execution_token, task.payload["duration_seconds"]

    def checkpoint(self, task_id, token, progress=0, processed=0, complete=False, error=None):
        with Session(self.engine) as db, db.begin():
            task = self.locked(db, task_id)
            timestamp = database_now(db)
            if (
                task.execution_token != token
                or task.status not in {"running", "cancel_requested"}
                or task.lease_until is None
                or task.lease_until <= timestamp
            ):
                return False
            if task.status == "cancel_requested":
                self.terminal(db, task, "cancelled")
                audit(db, task, "task.cancelled")
                return False
            if error:
                self.terminal(db, task, "failed", "TASK_EXECUTION_FAILED")
                audit(db, task, "task.failed")
                return False
            task.processed = min(max(0, processed), task.total)
            task.progress = min(99, max(0, progress))
            task.heartbeat_at = timestamp
            if complete:
                task.progress, task.processed = 100, task.total
                task.result_json = {
                    "duration_seconds": task.payload["duration_seconds"],
                    "ok": True,
                }
                self.terminal(db, task, "succeeded")
                audit(db, task, "task.succeeded")
            else:
                task.lease_until = timestamp + timedelta(seconds=self.settings.task_lease_seconds)
                task.version += 1
            return True

    def reconcile(self, limit=20):
        with Session(self.engine) as db, db.begin():
            timestamp = database_now(db)
            tasks = db.scalars(
                select(AsyncTask)
                .where(
                    or_(
                        (AsyncTask.status == "queued") & (AsyncTask.queued_deadline <= timestamp),
                        AsyncTask.status.in_(["running", "cancel_requested"])
                        & (AsyncTask.lease_until <= timestamp),
                    )
                )
                .order_by(AsyncTask.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for task in tasks:
                old_status = task.status
                if old_status == "cancel_requested":
                    self.terminal(db, task, "cancelled")
                    audit(db, task, "task.cancelled")
                    continue
                code = "TASK_QUEUE_TIMEOUT" if old_status == "queued" else "TASK_LEASE_EXPIRED"
                self.terminal(db, task, "failed", code)
                audit(db, task, "task.failed")
                if old_status == "running" and task.retry_count < self.settings.task_max_retries:
                    # Parent row lock and UNIQUE retry_of arbitrate with concurrent manual retry.
                    if not db.scalar(select(AsyncTask.id).where(AsyncTask.retry_of == task.id)):
                        self.new_task(
                            db,
                            task.actor_id,
                            "auto:" + task.id,
                            task.payload,
                            task.actor_id + ":diagnostic:retry:" + task.id,
                            task.request_id,
                            task,
                        )
            return len(tasks)
