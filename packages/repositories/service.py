"""Repository acceptance and fenced publication share the existing task/outbox transaction."""

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.persistence.models import AsyncTask, ParseCheckpoint, Repository
from packages.repositories.safety import RepositoryError, URLPolicy, canonicalize
from packages.repositories.storage import Storage
from packages.repositories.transport import RepositoryProbe
from packages.tasks.service import TaskError, TaskService, audit, database_now, key_valid, public


class RepositoryService:
    def __init__(self, settings, connections, probe=None, storage=None):
        self.settings, self.engine = settings, connections.engine
        self.tasks = TaskService(settings, connections)
        self.probe = probe or RepositoryProbe(URLPolicy())
        self.storage = storage or Storage(settings)

    @staticmethod
    def acknowledgement(task):
        return {"repository_id": task.payload["repository_id"], **TaskService.acknowledgement(task)}

    def existing(self, db, user, key, target):
        task = db.scalar(
            select(AsyncTask).where(
                AsyncTask.type == "repository.clone",
                AsyncTask.scope_key == user.id + ":repository:create",
                AsyncTask.idempotency_key == key,
            )
        )
        if task and task.payload["url"] != target.url:
            raise TaskError(409, "TASK_IDEMPOTENCY_CONFLICT")
        return task

    def create(self, user, key, value, request_id):
        key_valid(key)
        target = canonicalize(value)
        with Session(self.engine) as db, db.begin():
            self.tasks.actor(db, user)
            if task := self.existing(db, user, key, target):
                return self.acknowledgement(task)
        # Network checks happen before records and outside database/user locks.
        self.storage.preflight()
        self.probe.check(target)
        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if task := self.existing(db, user, key, target):
                    return self.acknowledgement(task)
                self.storage.preflight()
                repository_id = str(uuid4())
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {"repository_id": repository_id, "url": target.url},
                    user.id + ":repository:create",
                    request_id,
                    kind="repository.clone",
                )
                db.add(
                    Repository(
                        id=repository_id,
                        owner_id=user.id,
                        canonical_url=target.url,
                        latest_task_id=task.id,
                        status="queued",
                    )
                )
                db.flush()
                return self.acknowledgement(task)
        except IntegrityError as error:
            if error.orig.args[0] != 1062:
                raise
            with Session(self.engine) as db:
                if task := self.existing(db, user, key, target):
                    return self.acknowledgement(task)
                if db.scalar(select(Repository.id).where(Repository.canonical_url == target.url)):
                    raise RepositoryError(409, "REPOSITORY_ALREADY_EXISTS") from None
            raise

    @staticmethod
    def visible(repository, task, timestamp):
        return {
            "id": repository.id,
            "url": repository.canonical_url,
            "status": repository.status,
            "default_branch": repository.default_branch,
            "head_sha": repository.head_sha,
            "size_bytes": repository.size_bytes,
            "owner_id": repository.owner_id,
            "created_at": repository.created_at.isoformat(timespec="microseconds") + "Z",
            "task": public(task, timestamp),
            "commits_imported": repository.parse_status == "parsed",
            "parse_status": repository.parse_status,
        }

    def listing(self, page=1, page_size=20):
        with Session(self.engine) as db, db.begin():
            count = db.scalar(select(func.count()).select_from(Repository))
            records = db.execute(
                select(Repository, AsyncTask)
                .join(AsyncTask, Repository.latest_task_id == AsyncTask.id)
                .order_by(Repository.created_at.desc(), Repository.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            timestamp = database_now(db)
            return {
                "items": [self.visible(repo, task, timestamp) for repo, task in records],
                "total": count,
                "page": page,
                "page_size": page_size,
            }

    def detail(self, repository_id):
        with Session(self.engine) as db:
            repository = db.get(Repository, repository_id)
            if not repository:
                raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
            result = self.visible(
                repository, db.get(AsyncTask, repository.latest_task_id), database_now(db)
            )
            point = (
                db.get(ParseCheckpoint, repository.parse_root_task_id)
                if repository.parse_root_task_id
                else None
            )
            result["parse_window"] = (
                {
                    "head_sha": point.head_sha,
                    "commit_limit": point.commit_limit,
                    "parser_version": point.parser_version,
                    "processed": point.processed,
                    "total": point.total,
                    "checkpoint_sha": point.last_sha,
                }
                if point
                else None
            )
            return result

    def publish(self, task_id, token, metadata, storage_key):
        with Session(self.engine) as db, db.begin():
            task = self.tasks.locked(db, task_id)
            timestamp = database_now(db)
            if (
                task.execution_token != token
                or task.status not in {"running", "cancel_requested"}
                or task.lease_until is None
                or task.lease_until <= timestamp
            ):
                return False
            if task.status == "cancel_requested":
                self.tasks.terminal(db, task, "cancelled")
                audit(db, task, "task.cancelled")
                return False
            repository = db.get(Repository, task.payload["repository_id"], with_for_update=True)
            if not repository or repository.latest_task_id != task.id:
                return False
            repository.default_branch = metadata["default_branch"]
            repository.head_sha = metadata["head_sha"]
            repository.size_bytes = metadata["size_bytes"]
            repository.storage_key = storage_key
            task.result_json = {"repository_id": repository.id, **metadata}
            task.processed = metadata["size_bytes"]
            task.progress, task.heartbeat_at = 100, timestamp
            self.tasks.terminal(db, task, "succeeded")
            audit(db, task, "task.succeeded")
            return True

    def fail(self, task_id, token, code):
        with Session(self.engine) as db, db.begin():
            task = self.tasks.locked(db, task_id)
            timestamp = database_now(db)
            if (
                task.execution_token != token
                or task.status not in {"running", "cancel_requested"}
                or task.lease_until is None
                or task.lease_until <= timestamp
            ):
                return False
            self.tasks.terminal(
                db, task, "cancelled" if task.status == "cancel_requested" else "failed", code
            )
            audit(db, task, "task." + task.status)
            return True
