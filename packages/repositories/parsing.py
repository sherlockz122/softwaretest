"""Fenced parsing windows and atomic batches. Published Git data is never removed here."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.persistence.models import (
    AsyncTask,
    AuthorIdentity,
    FileChange,
    GitCommit,
    ParseCheckpoint,
    Repository,
)
from packages.repositories.parse_policy import PARSE_GROWTH, PARSER_VERSION
from packages.repositories.safety import RepositoryError
from packages.repositories.storage import Storage, safe_path
from packages.tasks.service import TaskService, audit, database_now, key_valid


class ParsingService:
    def __init__(self, settings, connections, storage=None):
        self.settings, self.engine = settings, connections.engine
        self.tasks = TaskService(settings, connections)
        self.storage = storage or Storage(settings)

    def create(self, user, repository_id, key, commit_limit, request_id):
        key_valid(key)
        scope = user.id + ":repository:parse:" + repository_id

        def existing(db):
            task = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "repository.parse",
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            if task and task.payload["commit_limit"] != commit_limit:
                raise RepositoryError(409, "TASK_IDEMPOTENCY_CONFLICT")
            return TaskService.acknowledgement(task) if task else None

        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := existing(db):
                    return replay
                repo = db.get(Repository, repository_id, with_for_update=True)
                if not repo:
                    raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
                # The row lock serializes different actors/keys for the same immutable window.
                if replay := existing(db):
                    return replay
                if repo.parse_root_task_id or repo.status != "cloned" or not repo.storage_key:
                    raise RepositoryError(409, "REPOSITORY_PARSE_STATE_CONFLICT")
                self.storage.capacity(PARSE_GROWTH)
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {
                        "repository_id": repo.id,
                        "commit_limit": commit_limit,
                        "parser_version": PARSER_VERSION,
                    },
                    scope,
                    request_id,
                    kind="repository.parse",
                )
                repo.parse_root_task_id = repo.latest_task_id = task.id
                repo.parse_status = "queued"
                db.add(
                    ParseCheckpoint(
                        root_task_id=task.id,
                        repository_id=repo.id,
                        head_sha=repo.head_sha,
                        commit_limit=commit_limit,
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
            raise RepositoryError(409, "REPOSITORY_PARSE_STATE_CONFLICT") from None
        except RepositoryError as error:
            if error.code != "REPOSITORY_PARSE_STATE_CONFLICT":
                raise
            # InnoDB REPEATABLE READ can retain the pre-lock idempotency snapshot.
            # Release the repository lock before a fresh read; workers lock task
            # then repository, so locking an active task here would invert ordering.
            with Session(self.engine) as db:
                if replay := existing(db):
                    return replay
            raise

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "repository.parse"
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
            or repo.parse_root_task_id != task.root_task_id
        ):
            return None
        point = db.get(ParseCheckpoint, task.root_task_id, with_for_update=True)
        if not point or point.parser_version != PARSER_VERSION:
            raise RepositoryError(409, "REPOSITORY_PARSE_VERSION_CONFLICT")
        return task, repo, point, now

    def source(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            task, repo, point, now = context
            key = repo.storage_key
            path = safe_path(self.storage.root / key) if key else None
            if (
                path is None
                or not path.is_relative_to(self.storage.root / "objects")
                or len(path.relative_to(self.storage.root).parts) != 4
                or path.name != "repo.git"
                or not path.is_dir()
            ):
                raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE")
            return path, point.head_sha, point.commit_limit

    def prepare(self, task_id, token, plan_hash, total):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            task, repo, point, now = context
            if point.plan_hash is not None and (
                point.plan_hash != plan_hash or point.total != total
            ):
                raise RepositoryError(409, "REPOSITORY_PARSE_PLAN_CONFLICT")
            point.plan_hash, point.total = plan_hash, total
            task.total, task.processed = total, point.processed
            task.progress = min(99, 100 * point.processed / max(total, 1))
            task.stage, task.heartbeat_at = "parsing", now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            return point.processed, point.last_sha

    def heartbeat(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, repo, point, now = context
            task.processed, task.total = point.processed, point.total
            task.progress = min(99, 100 * point.processed / max(point.total or 0, 1))
            task.stage, task.heartbeat_at = "parsing", now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            return True

    def batch(self, task_id, token, expected, records, plan_hash, complete=False):
        self.storage.capacity(PARSE_GROWTH)
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, repo, point, now = context
            if point.processed != expected or point.plan_hash != plan_hash:
                return False
            end = expected + len(records)
            if end > point.total or complete and end != point.total:
                raise RepositoryError(409, "REPOSITORY_PARSE_PLAN_CONFLICT")
            for record in records:
                identity = record["identity"]
                statement = insert(AuthorIdentity).values(id=str(uuid4()), **identity)
                db.execute(statement.on_duplicate_key_update(id=AuthorIdentity.id))
                author_id = db.scalar(
                    select(AuthorIdentity.id).where(
                        AuthorIdentity.identity_key == identity["identity_key"],
                        AuthorIdentity.identity_version == identity["identity_version"],
                    )
                )
                statement = insert(GitCommit).values(
                    id=str(uuid4()),
                    repository_id=repo.id,
                    author_identity_id=author_id,
                    **record["commit"],
                )
                db.execute(statement.on_duplicate_key_update(id=GitCommit.id))
                commit = db.scalar(
                    select(GitCommit).where(
                        GitCommit.repository_id == repo.id, GitCommit.sha == record["commit"]["sha"]
                    )
                )
                if commit.parser_version != point.parser_version:
                    raise RepositoryError(409, "REPOSITORY_PARSE_VERSION_CONFLICT")
                for change in record["files"]:
                    statement = insert(FileChange).values(
                        id=str(uuid4()), commit_id=commit.id, **change
                    )
                    db.execute(statement.on_duplicate_key_update(id=FileChange.id))
            point.processed = task.processed = end
            if records:
                point.last_sha = records[-1]["commit"]["sha"]
            task.progress = min(99, 100 * end / max(point.total, 1))
            task.total, task.heartbeat_at = point.total, now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            if complete:
                task.progress = 100
                task.result_json = {
                    "repository_id": repo.id,
                    "commits_imported": end,
                    "head_sha": point.head_sha,
                    "commit_limit": point.commit_limit,
                    "parser_version": point.parser_version,
                }
                self.tasks.terminal(db, task, "succeeded")
                audit(db, task, "task.succeeded")
            db.flush()
            return True

    def files(self, repository_id, sha, page=1, page_size=20):
        with Session(self.engine) as db:
            commit = db.scalar(
                select(GitCommit).where(
                    GitCommit.repository_id == repository_id, GitCommit.sha == sha
                )
            )
            if not commit:
                raise RepositoryError(404, "REPOSITORY_COMMIT_NOT_FOUND")
            query = select(FileChange).where(FileChange.commit_id == commit.id)
            count = db.scalar(
                select(func.count())
                .select_from(FileChange)
                .where(FileChange.commit_id == commit.id)
            )
            changes = db.scalars(
                query.order_by(FileChange.ordinal.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "total": count,
                "page": page,
                "page_size": page_size,
                "items": [
                    {
                        "ordinal": change.ordinal,
                        "old_path": change.old_path,
                        "new_path": change.new_path,
                        "change_type": change.change_type,
                        "insertions": change.insertions,
                        "deletions": change.deletions,
                        "old_loc": change.old_loc,
                        "is_binary": change.is_binary,
                        "content_status": change.content_status,
                        "parser_version": change.parser_version,
                    }
                    for change in changes
                ],
            }

    def listing(self, repository_id, page=1, page_size=20):
        with Session(self.engine) as db:
            if not db.get(Repository, repository_id):
                raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
            count = db.scalar(
                select(func.count())
                .select_from(GitCommit)
                .where(GitCommit.repository_id == repository_id)
            )
            commits = db.execute(
                select(GitCommit, AuthorIdentity)
                .join(AuthorIdentity, GitCommit.author_identity_id == AuthorIdentity.id)
                .where(GitCommit.repository_id == repository_id)
                .order_by(GitCommit.committer_time.asc(), GitCommit.sha.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "total": count,
                "page": page,
                "page_size": page_size,
                "items": [
                    {
                        "id": commit.id,
                        "sha": commit.sha,
                        "author": {"id": author.id, "name_alias": author.name_alias},
                        "author_time": commit.author_time.isoformat() + "Z",
                        "committer_time": commit.committer_time.isoformat() + "Z",
                        "message": commit.message[:4096],
                        "message_truncated": len(commit.message) > 4096,
                        "parents": commit.parents,
                        "parent_count": commit.parent_count,
                        "parse_status": commit.parse_status,
                        "parser_version": commit.parser_version,
                        "file_count": db.scalar(
                            select(func.count())
                            .select_from(FileChange)
                            .where(FileChange.commit_id == commit.id)
                        ),
                    }
                    for commit, author in commits
                ],
            }
