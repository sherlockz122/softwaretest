"""Immutable sync snapshots; only the final fenced transaction advances accepted HEAD."""

from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.persistence.models import AsyncTask, ParseCheckpoint, Repository, SyncWindow
from packages.repositories.clone import CloneExecutor, ExecutionStopped
from packages.repositories.parse_policy import PARSE_GROWTH, PARSER_VERSION
from packages.repositories.parser import ParseExecutor
from packages.repositories.parsing import ParsingService
from packages.repositories.safety import RepositoryError
from packages.repositories.service import RepositoryService
from packages.repositories.storage import safe_path
from packages.tasks.service import TaskService, audit, database_now, key_valid

SYNC_VERSION = "immutable-sync-v1"


class SyncService(ParsingService):
    def create(self, user, repository_id, key, request_id):
        key_valid(key)
        scope = user.id + ":repository:sync:" + repository_id

        def existing(db):
            task = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "repository.sync",
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            return TaskService.acknowledgement(task) if task else None

        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := existing(db):
                    return replay
                repo = db.get(Repository, repository_id, with_for_update=True)
                if not repo:
                    raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
                if (
                    repo.status != "cloned"
                    or repo.parse_status != "parsed"
                    or repo.sync_status not in {"pending", "synced"}
                    or not repo.storage_key
                    or repo.fix_status in {"queued", "detecting"}
                    or repo.szz_status in {"queued", "tracing"}
                ):
                    raise RepositoryError(409, "REPOSITORY_SYNC_STATE_CONFLICT")
                initial = db.get(ParseCheckpoint, repo.parse_root_task_id)
                if not initial or initial.parser_version != PARSER_VERSION:
                    raise RepositoryError(409, "REPOSITORY_PARSE_VERSION_CONFLICT")
                self.path(repo.storage_key, repo.id)
                self.storage.capacity(2 * self.settings.repository_max_bytes + PARSE_GROWTH)
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {
                        "repository_id": repo.id,
                        "url": repo.canonical_url,
                        "parser_version": PARSER_VERSION,
                        "sync_version": SYNC_VERSION,
                    },
                    scope,
                    request_id,
                    kind="repository.sync",
                )
                repo.latest_task_id = repo.sync_root_task_id = task.id
                repo.sync_status = "queued"
                db.add(
                    SyncWindow(
                        root_task_id=task.id,
                        repository_id=repo.id,
                        base_head_sha=repo.head_sha,
                        base_storage_key=repo.storage_key,
                        base_branch=repo.default_branch,
                        parser_version=PARSER_VERSION,
                    )
                )
                db.flush()
                return TaskService.acknowledgement(task)
        except IntegrityError as error:
            if error.orig.args[0] != 1062:
                raise
            with Session(self.engine) as db:
                if replay := existing(db):
                    return replay
            raise RepositoryError(409, "REPOSITORY_SYNC_STATE_CONFLICT") from None
        except RepositoryError as error:
            if error.code != "REPOSITORY_SYNC_STATE_CONFLICT":
                raise
            # Fresh read after releasing row lock avoids an InnoDB pre-lock snapshot.
            with Session(self.engine) as db:
                if replay := existing(db):
                    return replay
            raise

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "repository.sync"
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
        repo = db.get(Repository, task.payload["repository_id"], with_for_update=True)
        if (
            not repo
            or repo.latest_task_id != task.id
            or repo.sync_root_task_id != task.root_task_id
        ):
            return None
        point = db.get(SyncWindow, task.root_task_id, with_for_update=True)
        if (
            not point
            or point.parser_version != PARSER_VERSION
            or task.payload.get("sync_version") != SYNC_VERSION
        ):
            raise RepositoryError(409, "REPOSITORY_PARSE_VERSION_CONFLICT")
        if repo.head_sha != point.base_head_sha or repo.storage_key != point.base_storage_key:
            raise RepositoryError(409, "REPOSITORY_SYNC_BASE_CONFLICT")
        return task, repo, point, now

    def path(self, key, repo_id):
        path = safe_path(self.storage.root / key)
        if (
            not path.is_relative_to(self.storage.root / "objects")
            or len(path.relative_to(self.storage.root).parts) != 4
            or path.parent.parent.name != repo_id
            or path.name != "repo.git"
            or not path.is_dir()
        ):
            raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE")
        try:
            UUID(path.parent.name)
        except ValueError:
            raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE") from None
        return path

    def has_snapshot(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            return bool(context[2].storage_key) if context else None

    def publish(self, task_id, token, metadata, storage_key):
        self.storage.capacity(PARSE_GROWTH)
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, repo, point, now = context
            self.path(storage_key, repo.id)
            if point.storage_key is not None or storage_key == point.base_storage_key:
                return False
            point.storage_key = storage_key
            point.head_sha = metadata["head_sha"]
            point.default_branch = metadata["default_branch"]
            point.size_bytes = metadata["size_bytes"]
            # Publication is not task completion: retries must use this same snapshot.
            task.stage = "snapshot_ready"
            db.flush()
            return True

    def fail(self, task_id, token, code):
        return RepositoryService(self.settings, SimpleNamespace(engine=self.engine)).fail(
            task_id, token, code
        )

    def source(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            task, repo, point, now = context
            if not point.storage_key:
                raise RepositoryError(409, "REPOSITORY_SYNC_STATE_CONFLICT")
            return self.path(point.storage_key, repo.id), point.head_sha, None

    def plan(self, task_id, token, reader, head, commit_limit):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            point = context[2]
            base, old_branch, new_branch = (
                point.base_head_sha,
                point.base_branch,
                point.default_branch,
            )
        if old_branch is not None and old_branch != new_branch:
            relation = "requires_review"
        elif base == head:
            relation = "unchanged"
        elif base is None:
            relation = "initial"
        elif head is None or not reader.ancestor(base, head):
            relation = "requires_review"
        else:
            relation = "fast_forward"
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            task, repo, point, now = context
            if point.relation not in {"pending", relation}:
                raise RepositoryError(409, "REPOSITORY_PARSE_PLAN_CONFLICT")
            point.relation = relation
            if relation == "requires_review":
                point.total = task.total = task.processed = 0
                repo.sync_status = "requires_review"
                task.progress = 100
                task.result_json = {
                    "repository_id": repo.id,
                    "disposition": relation,
                    "base_head_sha": base,
                    "head_sha": head,
                    "default_branch": new_branch,
                    "commits_imported": 0,
                }
                self.tasks.terminal(db, task, "succeeded")
                audit(db, task, "repository.sync.requires_review")
        if relation == "requires_review":
            raise ExecutionStopped
        return reader.plan(head, None, exclude=base) if relation != "unchanged" else []

    def finish(self, db, task, repo, point, end):
        if point.relation not in {"initial", "unchanged", "fast_forward"}:
            raise RepositoryError(409, "REPOSITORY_SYNC_STATE_CONFLICT")
        self.path(point.storage_key, repo.id)
        repo.head_sha, repo.storage_key = point.head_sha, point.storage_key
        repo.default_branch, repo.size_bytes = point.default_branch, point.size_bytes
        task.result_json = {
            "repository_id": repo.id,
            "disposition": point.relation,
            "base_head_sha": point.base_head_sha,
            "head_sha": point.head_sha,
            "commits_imported": end,
            "parser_version": point.parser_version,
        }
        self.tasks.terminal(db, task, "succeeded")
        audit(db, task, "task.succeeded")

    @staticmethod
    def visible(point):
        return {
            "root_task_id": point.root_task_id,
            "base_head_sha": point.base_head_sha,
            "head_sha": point.head_sha,
            "default_branch": point.default_branch,
            "relation": point.relation,
            "parser_version": point.parser_version,
            "processed": point.processed,
            "total": point.total,
            "checkpoint_sha": point.last_sha,
            "created_at": point.created_at.isoformat() + "Z",
        }

    def windows(self, repo_id, page=1, page_size=20):
        with Session(self.engine) as db:
            if not db.get(Repository, repo_id):
                raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
            condition = SyncWindow.repository_id == repo_id
            count = db.scalar(select(func.count()).select_from(SyncWindow).where(condition))
            points = db.scalars(
                select(SyncWindow)
                .where(condition)
                .order_by(SyncWindow.created_at.desc(), SyncWindow.root_task_id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "total": count,
                "page": page,
                "page_size": page_size,
                "items": [self.visible(point) for point in points],
            }


class SyncExecutor:
    def __init__(self, settings, service, clone=None):
        self.settings, self.service = settings, service
        self.clone = clone or CloneExecutor(settings, service)

    def run(self, task_id, token, payload):
        try:
            ready = self.service.has_snapshot(task_id, token)
            if ready is None:
                return False
            if not ready:
                self.service.storage.capacity(2 * self.settings.repository_max_bytes + PARSE_GROWTH)
            if not ready and not self.clone.run(task_id, token, payload):
                return False
            return ParseExecutor(self.settings, self.service).run(task_id, token, payload)
        except RepositoryError as error:
            self.service.fail(task_id, token, error.code)
            return False
