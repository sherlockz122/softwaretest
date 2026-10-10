"""Frozen SZZ inputs and fenced, atomic publication of line evidence."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.mining.fix import FixService
from packages.mining.szz_engine import ALGORITHM_HASH, ALGORITHM_VERSION, trace
from packages.persistence.models import (
    AsyncTask,
    DefectEvidence,
    FileChange,
    FixAssessment,
    FixRun,
    GitCommit,
    Repository,
    SZZItem,
    SZZLink,
    SZZRun,
)
from packages.repositories.clone import ExecutionStopped
from packages.repositories.parse_policy import PARSE_GROWTH, PARSER_VERSION
from packages.repositories.safety import RepositoryError
from packages.tasks.service import TaskService, audit, database_now, key_valid


def iso(value):
    return value.isoformat(timespec="microseconds") + "Z"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SZZService(FixService):
    def create(self, user, repository_id, key, policy, request_id):
        key_valid(key)
        if policy["algorithm_version"] != ALGORITHM_VERSION:
            raise RepositoryError(422, "SZZ_VERSION_CONFLICT")
        scope = user.id + ":repository:szz:" + repository_id

        def existing(db):
            task = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "repository.szz",
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            if task and task.payload["request_policy"] != policy:
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
                    or repo.fix_status != "detected"
                    or repo.sync_status not in {"pending", "synced", "requires_review"}
                    or repo.szz_status in {"queued", "tracing"}
                ):
                    raise RepositoryError(409, "SZZ_STATE_CONFLICT")
                fix = db.get(FixRun, policy["fix_run_id"], with_for_update=True)
                if (
                    not fix
                    or fix.repository_id != repo.id
                    or fix.head_sha != repo.head_sha
                    or fix.storage_key != repo.storage_key
                    or fix.total is None
                    or fix.total != fix.processed
                    or fix.parser_version != PARSER_VERSION
                ):
                    raise RepositoryError(409, "SZZ_SOURCE_CONFLICT")
                now = database_now(db)
                cutoff = (
                    datetime.fromisoformat(policy["as_of"]).astimezone(UTC).replace(tzinfo=None)
                    if policy["as_of"]
                    else now
                )
                if cutoff > now:
                    raise RepositoryError(422, "SZZ_CUTOFF_FUTURE")
                self.storage.capacity(PARSE_GROWTH)
                rows = db.scalars(
                    select(FixAssessment)
                    .where(FixAssessment.root_task_id == fix.root_task_id)
                    .order_by(FixAssessment.sha)
                    .with_for_update()
                ).all()
                if len(rows) != fix.total:
                    raise RepositoryError(409, "SZZ_SOURCE_CONFLICT")
                frozen = []
                for row in rows:
                    commit = db.get(GitCommit, row.commit_id)
                    evidence = list(
                        db.scalars(
                            select(DefectEvidence)
                            .where(DefectEvidence.assessment_id == row.id)
                            .order_by(DefectEvidence.ordinal)
                        )
                    )
                    times = [fix.created_at, row.created_at, commit.committer_time]
                    if row.reviewed_at:
                        times.append(row.reviewed_at)
                    for e in evidence:
                        if observed := e.source.get("observed_at"):
                            times.append(
                                datetime.fromisoformat(observed)
                                .astimezone(UTC)
                                .replace(tzinfo=None)
                            )
                    available = max(times)
                    source = {
                        "assessment_id": row.id,
                        "commit_id": commit.id,
                        "sha": row.sha,
                        "candidate": row.review_status == "confirmed"
                        or row.review_status == "unreviewed"
                        and row.rule_candidate,
                        "rule_candidate": row.rule_candidate,
                        "review_status": row.review_status,
                        "review_revision": row.review_revision,
                        "rule_version": fix.rule_version,
                        "rule_hash": fix.rule_hash,
                        "available_at": iso(available),
                        "visible": available <= cutoff,
                        "confidence": "reviewed"
                        if row.review_status == "confirmed"
                        else "high"
                        if any(e.confidence == "high" for e in evidence)
                        else "medium"
                        if row.rule_candidate
                        else "none",
                        "evidence_digest": digest(
                            [
                                {
                                    "type": e.type,
                                    "value": e.value,
                                    "confidence": e.confidence,
                                    "source": e.source,
                                }
                                for e in evidence
                            ]
                        ),
                    }
                    frozen.append(source)
                source_digest = digest(frozen)
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {
                        "repository_id": repo.id,
                        "szz_version": ALGORITHM_VERSION,
                        "request_policy": policy,
                    },
                    scope,
                    request_id,
                    kind="repository.szz",
                )
                repo.latest_task_id = repo.szz_root_task_id = task.id
                repo.szz_status = "queued"
                db.add(
                    SZZRun(
                        root_task_id=task.id,
                        repository_id=repo.id,
                        fix_root_task_id=fix.root_task_id,
                        head_sha=fix.head_sha,
                        storage_key=fix.storage_key,
                        parser_version=fix.parser_version,
                        history_coverage=fix.history_coverage,
                        algorithm_version=ALGORITHM_VERSION,
                        algorithm_hash=ALGORITHM_HASH,
                        source_digest=source_digest,
                        as_of=cutoff,
                        label_version=digest(
                            {
                                "algorithm": ALGORITHM_HASH,
                                "fix_run": fix.root_task_id,
                                "head_sha": fix.head_sha,
                                "include_medium": fix.include_medium,
                                "rule": fix.rule_hash,
                                "parser": fix.parser_version,
                                "coverage": fix.history_coverage,
                                "as_of": iso(cutoff),
                                "source": source_digest,
                            }
                        ),
                    )
                )
                db.flush()
                for source in frozen:
                    db.add(
                        SZZItem(
                            id=str(uuid4()),
                            root_task_id=task.id,
                            assessment_id=source["assessment_id"],
                            sha=source["sha"],
                            input_json=source,
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
            raise RepositoryError(409, "SZZ_STATE_CONFLICT") from None
        except RepositoryError as error:
            if error.code != "SZZ_STATE_CONFLICT":
                raise
            with Session(self.engine) as db:
                if replay := existing(db):
                    return replay
            raise

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "repository.szz"
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
        point = db.get(SZZRun, task.root_task_id, with_for_update=True)
        if (
            not repo
            or not point
            or repo.latest_task_id != task.id
            or repo.szz_root_task_id != task.root_task_id
        ):
            return None
        if (
            point.algorithm_hash != ALGORITHM_HASH
            or point.algorithm_version != ALGORITHM_VERSION
            or point.parser_version != PARSER_VERSION
            or point.head_sha != repo.head_sha
            or point.storage_key != repo.storage_key
        ):
            raise RepositoryError(409, "SZZ_VERSION_CONFLICT")
        return task, repo, point, now

    def plan(self, task_id, token, reader, head, commit_limit):
        version = reader.command(["--version"]).decode().strip()
        start = int(reader.environment["GIT_CONFIG_COUNT"])
        for index, (key, value) in enumerate(
            {
                "blame.ignoreRevsFile": "",
                "blame.blankBoundary": "false",
                "core.quotePath": "true",
            }.items(),
            start,
        ):
            reader.environment[f"GIT_CONFIG_KEY_{index}"] = key
            reader.environment[f"GIT_CONFIG_VALUE_{index}"] = value
        reader.environment["GIT_CONFIG_COUNT"] = str(start + 3)
        reachable = set(reader.plan(head, None))
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            point = context[2]
            if point.git_version and point.git_version != version:
                raise RepositoryError(409, "SZZ_VERSION_CONFLICT")
            point.git_version = version
            reader.szz_eligible = {
                sha: timestamp
                for sha, timestamp in db.execute(
                    select(GitCommit.sha, GitCommit.committer_time).where(
                        GitCommit.repository_id == point.repository_id,
                        GitCommit.parser_version == point.parser_version,
                    )
                )
                if sha in reachable
            }
            if point.history_coverage == "full" and set(reader.szz_eligible) != reachable:
                raise RepositoryError(409, "SZZ_SOURCE_CONFLICT")
            inputs = list(
                db.scalars(
                    select(SZZItem)
                    .where(SZZItem.root_task_id == point.root_task_id)
                    .order_by(SZZItem.sha)
                )
            )
            if digest([row.input_json for row in inputs]) != point.source_digest or any(
                row.sha not in reachable for row in inputs
            ):
                raise RepositoryError(409, "SZZ_SOURCE_CONFLICT")
            return [row.sha for row in inputs]

    def record(self, task_id, token, reader, sha):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            _, repo, point, _ = context
            item = db.scalar(
                select(SZZItem).where(
                    SZZItem.root_task_id == point.root_task_id, SZZItem.sha == sha
                )
            )
            commit = db.get(GitCommit, item.input_json["commit_id"])
            files = [
                {
                    k: getattr(f, k)
                    for k in (
                        "ordinal",
                        "old_path",
                        "new_path",
                        "old_blob",
                        "content_status",
                        "is_binary",
                        "line_numbers",
                    )
                }
                for f in db.scalars(
                    select(FileChange)
                    .where(FileChange.commit_id == commit.id)
                    .order_by(FileChange.ordinal)
                )
            ]
            source = item.input_json
            data = {
                k: getattr(commit, k) for k in ("sha", "parents", "parse_status", "committer_time")
            }
        # Exclude persisted but unaccepted synchronization batches from label eligibility.
        output = trace(reader, source, data, files, reader.szz_eligible)
        for link in output["links"]:
            link["confidence"] = source["confidence"]
        return {"sha": sha, "output": output}

    def heartbeat(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, _, point, now = context
            task.processed, task.total = point.processed, point.total
            task.progress = min(99, 100 * point.processed / max(point.total or 0, 1))
            task.stage, task.heartbeat_at = "tracing_szz", now
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
                raise RepositoryError(409, "SZZ_SOURCE_CONFLICT")
            for record in records:
                item = db.scalar(
                    select(SZZItem)
                    .where(SZZItem.root_task_id == point.root_task_id, SZZItem.sha == record["sha"])
                    .with_for_update()
                )
                if item.result_json is not None:
                    raise RepositoryError(409, "SZZ_SOURCE_CONFLICT")
                output = record["output"]
                item.result_json = {k: v for k, v in output.items() if k != "links"}
                for ordinal, link in enumerate(output["links"]):
                    db.add(
                        SZZLink(id=str(uuid4()), item_id=item.id, ordinal=ordinal, evidence=link)
                    )
            point.processed = end
            if records:
                point.last_sha = records[-1]["sha"]
            task.processed, task.total, task.heartbeat_at = end, point.total, now
            task.progress = 100 if complete else min(99, 100 * end / max(point.total, 1))
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.stage = "tracing_szz"
            task.version += 1
            if complete:
                task.result_json = {
                    "repository_id": repo.id,
                    "run_id": point.root_task_id,
                    "head_sha": point.head_sha,
                    "label_version": point.label_version,
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
                "fix_root_task_id",
                "head_sha",
                "parser_version",
                "history_coverage",
                "algorithm_version",
                "algorithm_hash",
                "label_version",
                "source_digest",
                "git_version",
                "processed",
                "total",
            )
        } | {"created_at": iso(point.created_at), "as_of": iso(point.as_of)}

    def runs(self, repository_id, page=1, page_size=20):
        with Session(self.engine) as db:
            if not db.get(Repository, repository_id):
                raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
            count = db.scalar(
                select(func.count())
                .select_from(SZZRun)
                .where(SZZRun.repository_id == repository_id)
            )
            rows = db.scalars(
                select(SZZRun)
                .where(SZZRun.repository_id == repository_id)
                .order_by(SZZRun.created_at.desc(), SZZRun.root_task_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "items": [self.visible(row) for row in rows],
                "total": count,
                "page": page,
                "page_size": page_size,
            }

    def results(self, run_id, page=1, page_size=20):
        with Session(self.engine) as db:
            point = db.get(SZZRun, run_id)
            if not point:
                raise RepositoryError(404, "SZZ_RUN_NOT_FOUND")
            count = db.scalar(
                select(func.count()).select_from(SZZItem).where(SZZItem.root_task_id == run_id)
            )
            rows = list(
                db.scalars(
                    select(SZZItem)
                    .where(SZZItem.root_task_id == run_id)
                    .order_by(SZZItem.sha)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            return {
                "run": self.visible(point),
                "items": [
                    {
                        "id": row.id,
                        "sha": row.sha,
                        "input": {k: v for k, v in row.input_json.items() if k != "commit_id"},
                        "result": row.result_json,
                        "links_count": db.scalar(
                            select(func.count())
                            .select_from(SZZLink)
                            .where(SZZLink.item_id == row.id)
                        ),
                    }
                    for row in rows
                ],
                "total": count,
                "page": page,
                "page_size": page_size,
            }

    def links(self, run_id, page=1, page_size=20):
        with Session(self.engine) as db:
            point = db.get(SZZRun, run_id)
            if not point:
                raise RepositoryError(404, "SZZ_RUN_NOT_FOUND")
            condition = SZZItem.root_task_id == run_id
            count = db.scalar(
                select(func.count())
                .select_from(SZZLink)
                .join(SZZItem, SZZItem.id == SZZLink.item_id)
                .where(condition)
            )
            rows = db.scalars(
                select(SZZLink)
                .join(SZZItem, SZZItem.id == SZZLink.item_id)
                .where(condition)
                .order_by(SZZItem.sha, SZZLink.ordinal)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "run": self.visible(point),
                "items": [row.evidence for row in rows],
                "total": count,
                "page": page,
                "page_size": page_size,
            }
