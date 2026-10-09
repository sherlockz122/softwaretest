"""Accepted-snapshot Fix runs, immutable Issue observations and fenced atomic batches."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.mining.fix_rules import RULE_HASH, RULE_VERSION, evaluate, references
from packages.mining.issues import GitHubIssues, observation, repository_name
from packages.persistence.models import (
    AsyncTask,
    DefectEvidence,
    FileChange,
    FixAssessment,
    FixRun,
    GitCommit,
    IssueObservation,
    OperationLog,
    ParseCheckpoint,
    Repository,
)
from packages.repositories.clone import ExecutionStopped
from packages.repositories.parse_policy import PARSE_GROWTH, PARSER_VERSION
from packages.repositories.parsing import ParsingService
from packages.repositories.safety import RepositoryError
from packages.repositories.storage import safe_path
from packages.tasks.service import TaskService, audit, database_now, key_valid


class FixService(ParsingService):
    def __init__(self, settings, connections, storage=None, issues=None):
        super().__init__(settings, connections, storage)
        self.issues = issues or GitHubIssues()

    def create(self, user, repository_id, key, policy, request_id):
        key_valid(key)
        if policy["rule_version"] != RULE_VERSION or type(policy["include_medium"]) is not bool:
            raise RepositoryError(422, "FIX_RULE_UNSUPPORTED")
        scope = user.id + ":repository:fix:" + repository_id

        def existing(db):
            task = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "repository.fix",
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            if task and any(task.payload[k] != v for k, v in policy.items()):
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
                if (
                    repo.status != "cloned"
                    or repo.parse_status != "parsed"
                    or repo.sync_status not in {"pending", "synced", "requires_review"}
                    or repo.fix_status in {"queued", "detecting"}
                ):
                    raise RepositoryError(409, "FIX_STATE_CONFLICT")
                checkpoint = db.get(ParseCheckpoint, repo.parse_root_task_id, with_for_update=True)
                if not checkpoint or checkpoint.parser_version != PARSER_VERSION:
                    raise RepositoryError(409, "FIX_VERSION_CONFLICT")
                self.storage.capacity(PARSE_GROWTH)
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {"repository_id": repo.id, "fix_version": RULE_VERSION, **policy},
                    scope,
                    request_id,
                    kind="repository.fix",
                )
                repo.fix_root_task_id = repo.latest_task_id = task.id
                repo.fix_status = "queued"
                db.add(
                    FixRun(
                        root_task_id=task.id,
                        repository_id=repo.id,
                        head_sha=repo.head_sha,
                        storage_key=repo.storage_key,
                        parser_version=PARSER_VERSION,
                        history_coverage="recent_window" if checkpoint.commit_limit else "full",
                        rule_version=RULE_VERSION,
                        rule_hash=RULE_HASH,
                        include_medium=policy["include_medium"],
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
            raise RepositoryError(409, "FIX_STATE_CONFLICT") from None
        except RepositoryError as error:
            if error.code != "FIX_STATE_CONFLICT":
                raise
            with Session(self.engine) as db:
                if replay := existing(db):
                    return replay
            raise

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "repository.fix"
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
        point = db.get(FixRun, task.root_task_id, with_for_update=True)
        if (
            not repo
            or not point
            or repo.latest_task_id != task.id
            or repo.fix_root_task_id != task.root_task_id
        ):
            return None
        if (
            point.rule_version != RULE_VERSION
            or point.rule_hash != RULE_HASH
            or point.parser_version != PARSER_VERSION
            or repo.head_sha != point.head_sha
            or repo.storage_key != point.storage_key
        ):
            raise RepositoryError(409, "FIX_VERSION_CONFLICT")
        return task, repo, point, now

    def source(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            _, repo, point, _ = context
            path = safe_path(self.storage.root / point.storage_key)
            parts = path.relative_to(self.storage.root).parts
            if (
                len(parts) != 4
                or parts[0] != "objects"
                or parts[1] != repo.id
                or parts[3] != "repo.git"
                or not path.is_dir()
            ):
                raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE")
            return path, point.head_sha, None

    def plan(self, task_id, token, reader, head, commit_limit):
        reachable = reader.plan(head, None)
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            _, repo, point, _ = context
            imported = set(
                db.scalars(
                    select(GitCommit.sha).where(
                        GitCommit.repository_id == repo.id,
                        GitCommit.parser_version == PARSER_VERSION,
                    )
                )
            )
            if point.history_coverage == "full" and any(sha not in imported for sha in reachable):
                raise RepositoryError(409, "FIX_PLAN_CONFLICT")
            return [sha for sha in reachable if sha in imported]

    def observe(self, task_id, token, url, number):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            point = context[2]
            root = point.root_task_id
            if row := db.get(IssueObservation, (root, number)):
                return row.snapshot
            count = db.scalar(
                select(func.count())
                .select_from(IssueObservation)
                .where(IssueObservation.root_task_id == root)
            )
        # No network I/O under database locks; immutable publication checks lease again.
        snapshot = (
            self.issues.lookup(url, number)
            if count < 256
            else observation("request_budget", number)
        )
        with Session(self.engine) as db, db.begin():
            if not self.fenced(db, task_id, token):
                raise ExecutionStopped
            row = db.get(IssueObservation, (root, number))
            if row:
                return row.snapshot
            db.add(IssueObservation(root_task_id=root, number=number, snapshot=snapshot))
            return snapshot

    def record(self, task_id, token, reader, sha):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            _, repo, point, _ = context
            commit = db.scalar(
                select(GitCommit).where(
                    GitCommit.repository_id == repo.id,
                    GitCommit.sha == sha,
                    GitCommit.parser_version == PARSER_VERSION,
                )
            )
            if not commit:
                raise RepositoryError(409, "FIX_PLAN_CONFLICT")
            file_rows = (
                db.execute(
                    select(
                        FileChange.old_path, FileChange.new_path, FileChange.content_status
                    ).where(FileChange.commit_id == commit.id)
                )
                .mappings()
                .all()
            )
            data = dict(
                id=commit.id,
                sha=sha,
                message=commit.message,
                parse_status=commit.parse_status,
                files=[dict(f) for f in file_rows],
            )
            url, include_medium = repo.canonical_url, point.include_medium
        refs = references(data["message"], repository_name(url))
        for ref in refs:
            reader.tick()
            ref["snapshot"] = (
                self.observe(task_id, token, url, ref["number"])
                if ref["local"]
                else observation("external_reference", ref["number"])
            )
            reader.tick()
        return {
            "commit_id": data["id"],
            "sha": sha,
            **evaluate(data["message"], data["parse_status"], data["files"], refs, include_medium),
        }

    def prepare(self, task_id, token, plan_hash, total):
        result = super().prepare(task_id, token, plan_hash, total)
        self.heartbeat(task_id, token)
        return result

    def heartbeat(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, _, point, now = context
            task.processed, task.total = point.processed, point.total
            task.progress = min(99, 100 * point.processed / max(point.total or 0, 1))
            task.stage, task.heartbeat_at = "detecting_fix", now
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
                raise RepositoryError(409, "FIX_PLAN_CONFLICT")
            for record in records:
                assessment = FixAssessment(
                    id=str(uuid4()),
                    root_task_id=point.root_task_id,
                    commit_id=record["commit_id"],
                    sha=record["sha"],
                    rule_candidate=record["rule_candidate"],
                    disposition=record["disposition"],
                    content_complete=record["content_complete"],
                )
                db.add(assessment)
                db.flush()
                for ordinal, evidence in enumerate(record["evidence"]):
                    db.add(
                        DefectEvidence(
                            id=str(uuid4()),
                            assessment_id=assessment.id,
                            fix_commit_id=assessment.commit_id,
                            ordinal=ordinal,
                            rule_version=point.rule_version,
                            **evidence,
                        )
                    )
            point.processed = end
            if records:
                point.last_sha = records[-1]["sha"]
            task.processed, task.total, task.heartbeat_at = end, point.total, now
            task.stage = "detecting_fix"
            task.progress = 100 if complete else min(99, 100 * end / max(point.total, 1))
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            if complete:
                task.result_json = {
                    "repository_id": repo.id,
                    "head_sha": point.head_sha,
                    "rule_version": point.rule_version,
                    "run_id": point.root_task_id,
                    "assessed": end,
                    "history_coverage": point.history_coverage,
                }
                self.tasks.terminal(db, task, "succeeded")
                audit(db, task, "task.succeeded")
            db.flush()
            return True

    @staticmethod
    def visible(point):
        return {
            k: getattr(point, k)
            for k in (
                "root_task_id",
                "head_sha",
                "parser_version",
                "history_coverage",
                "rule_version",
                "rule_hash",
                "include_medium",
                "processed",
                "total",
            )
        } | {"created_at": point.created_at.isoformat() + "Z"}

    def runs(self, repository_id, page=1, page_size=20):
        with Session(self.engine) as db, db.begin():
            if not db.get(Repository, repository_id):
                raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
            query = select(FixRun).where(FixRun.repository_id == repository_id)
            count = db.scalar(
                select(func.count())
                .select_from(FixRun)
                .where(FixRun.repository_id == repository_id)
            )
            rows = db.scalars(
                query.order_by(FixRun.created_at.desc(), FixRun.root_task_id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "items": [self.visible(row) for row in rows],
                "total": count,
                "page": page,
                "page_size": page_size,
            }

    @staticmethod
    def assessment(db, row):
        evidence = db.scalars(
            select(DefectEvidence)
            .where(DefectEvidence.assessment_id == row.id)
            .order_by(DefectEvidence.ordinal)
        )
        return {
            "id": row.id,
            "sha": row.sha,
            "rule_candidate": row.rule_candidate,
            "candidate": row.review_status == "confirmed"
            or row.review_status == "unreviewed"
            and row.rule_candidate,
            "disposition": row.disposition,
            "content_complete": row.content_complete,
            "review_status": row.review_status,
            "review_revision": row.review_revision,
            "review_note": row.review_note,
            "reviewed_at": row.reviewed_at.isoformat() + "Z" if row.reviewed_at else None,
            "review_history": [
                log.detail_json
                for log in db.scalars(
                    select(OperationLog)
                    .where(
                        OperationLog.object_type == "fix_assessment",
                        OperationLog.object_id == row.id,
                        OperationLog.action == "fix.review",
                    )
                    .order_by(OperationLog.created_at.desc(), OperationLog.id)
                    .limit(20)
                )
            ],
            "evidence": [
                {
                    "type": e.type,
                    "value": e.value,
                    "confidence": e.confidence,
                    "rule_version": e.rule_version,
                    "source": e.source,
                }
                for e in evidence
            ],
        }

    def results(self, repository_id, run_id, page=1, page_size=20):
        with Session(self.engine) as db, db.begin():
            point = db.get(FixRun, run_id)
            if not point or point.repository_id != repository_id:
                raise RepositoryError(404, "FIX_RUN_NOT_FOUND")
            count = db.scalar(
                select(func.count())
                .select_from(FixAssessment)
                .where(FixAssessment.root_task_id == run_id)
            )
            rows = db.scalars(
                select(FixAssessment)
                .where(FixAssessment.root_task_id == run_id)
                .order_by(FixAssessment.sha)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "run": self.visible(point),
                "items": [self.assessment(db, row) for row in rows],
                "total": count,
                "page": page,
                "page_size": page_size,
            }

    def review(self, user, repository_id, assessment_id, body, request_id):
        with Session(self.engine) as db, db.begin():
            self.tasks.actor(db, user)
            row = db.get(FixAssessment, assessment_id, with_for_update=True)
            if not row:
                raise RepositoryError(404, "FIX_EVIDENCE_NOT_FOUND")
            point = db.get(FixRun, row.root_task_id)
            if point.repository_id != repository_id:
                raise RepositoryError(404, "FIX_EVIDENCE_NOT_FOUND")
            if body["expected_revision"] != row.review_revision:
                if (
                    row.review_revision == body["expected_revision"] + 1
                    and row.review_actor_id == user.id
                    and row.review_status == body["status"]
                    and row.review_note == body["note"]
                ):
                    return self.assessment(db, row)
                raise RepositoryError(409, "FIX_REVIEW_CONFLICT")
            row.review_status, row.review_note = body["status"], body["note"]
            row.review_actor_id, row.reviewed_at = user.id, database_now(db)
            row.review_revision += 1
            db.add(
                OperationLog(
                    actor_id=user.id,
                    action="fix.review",
                    object_type="fix_assessment",
                    object_id=row.id,
                    result="succeeded",
                    request_id=request_id,
                    detail_json={
                        "run_id": point.root_task_id,
                        "rule_version": point.rule_version,
                        "status": row.review_status,
                        "revision": row.review_revision,
                        "note": row.review_note,
                    },
                )
            )
            db.flush()
            return self.assessment(db, row)
