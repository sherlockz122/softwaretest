"""Accepted-snapshot feature runs; immutable inputs and fenced atomic batches."""

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.mining import feature_versions
from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import (
    FEATURE_VERSION,
    CommitInput,
    FeatureInputError,
    FileDelta,
    FixSignal,
    Snapshot,
    compute,
    digest,
    quality,
)
from packages.mining.fix import FixService
from packages.mining.szz import iso
from packages.persistence.models import (
    AsyncTask,
    AuthorIdentity,
    DefectEvidence,
    FeatureItem,
    FeatureRun,
    FileChange,
    FixAssessment,
    FixRun,
    GitCommit,
    ParseCheckpoint,
    Repository,
)
from packages.repositories.clone import ExecutionStopped
from packages.repositories.parse_policy import IDENTITY_VERSION, PARSE_GROWTH, PARSER_VERSION
from packages.repositories.safety import RepositoryError
from packages.tasks.service import TaskService, audit, database_now, key_valid


def encode_input(commit):
    return json.loads(json.dumps(asdict(commit), default=iso))


def decode_input(data):
    data = dict(data)
    data["event_time"] = datetime.fromisoformat(data["event_time"].removesuffix("Z"))
    data["parents"] = tuple(data["parents"])
    data["files"] = tuple(FileDelta(**f) for f in data["files"])
    if data["fix"]:
        fix = dict(data["fix"])
        fix["available_at"] = datetime.fromisoformat(fix["available_at"].removesuffix("Z"))
        data["fix"] = FixSignal(**fix)
    return CommitInput(**data)


class FeatureService(FixService):
    def create(self, user, repository_id, key, policy, request_id):
        key_valid(key)
        version = policy["feature_version"]
        if version not in (FEATURE_VERSION, feature_versions.V2) or policy["purpose"] not in (
            "historical",
            "candidate",
        ):
            raise RepositoryError(422, "FEATURE_POLICY_UNSUPPORTED")
        if version == feature_versions.V2 and policy["fix_run_id"] is not None:
            raise RepositoryError(422, "FEATURE_POLICY_UNSUPPORTED")
        scope = user.id + ":repository:feature:" + repository_id

        def existing(db):
            task = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "repository.feature",
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
                    or not repo.head_sha
                    or repo.sync_status not in {"pending", "synced", "requires_review"}
                    or repo.feature_status in {"queued", "extracting"}
                    or repo.fix_status in {"queued", "detecting"}
                    or repo.szz_status in {"queued", "tracing"}
                ):
                    raise RepositoryError(409, "FEATURE_STATE_CONFLICT")
                checkpoint = db.get(ParseCheckpoint, repo.parse_root_task_id)
                if not checkpoint or checkpoint.parser_version != PARSER_VERSION:
                    raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
                frozen = {}
                fixed = None
                if policy["fix_run_id"]:
                    fixed = db.get(FixRun, policy["fix_run_id"], with_for_update=True)
                    if (
                        not fixed
                        or fixed.repository_id != repo.id
                        or fixed.head_sha != repo.head_sha
                        or fixed.storage_key != repo.storage_key
                        or fixed.total is None
                        or fixed.total != fixed.processed
                        or fixed.parser_version != PARSER_VERSION
                    ):
                        raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
                    rows = db.scalars(
                        select(FixAssessment)
                        .where(FixAssessment.root_task_id == fixed.root_task_id)
                        .order_by(FixAssessment.sha)
                        .with_for_update()
                    ).all()
                    if len(rows) != fixed.total:
                        raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
                    for row in rows:
                        evidence = list(
                            db.scalars(
                                select(DefectEvidence)
                                .where(DefectEvidence.assessment_id == row.id)
                                .order_by(DefectEvidence.ordinal)
                                .with_for_update(read=True)
                            )
                        )
                        times = [fixed.created_at, row.created_at]
                        if row.reviewed_at:
                            times.append(row.reviewed_at)
                        for item in evidence:
                            if observed := item.source.get("observed_at"):
                                times.append(
                                    datetime.fromisoformat(observed.replace("Z", "+00:00"))
                                    .astimezone(UTC)
                                    .replace(tzinfo=None)
                                )
                        # Confirmed/rejected reviews are explicit signals. Unreviewed
                        # incomplete observations cannot prove a negative FIX.
                        known = row.review_status != "unreviewed" or row.content_complete
                        frozen[row.sha] = {
                            "signal": {
                                "value": row.review_status == "confirmed"
                                or (row.review_status == "unreviewed" and row.rule_candidate),
                                "available_at": iso(max(times)),
                                "rule_version": fixed.rule_version,
                                "review_revision": row.review_revision,
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
                            if known
                            else None,
                            "review_status": row.review_status,
                            "review_revision": row.review_revision,
                        }
                self.storage.capacity(PARSE_GROWTH)
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {
                        "repository_id": repo.id,
                        "feature_version": version,
                        "request_policy": policy,
                    },
                    scope,
                    request_id,
                    kind="repository.feature",
                )
                repo.latest_task_id = repo.feature_root_task_id = task.id
                repo.feature_status = "queued"
                db.add(
                    FeatureRun(
                        root_task_id=task.id,
                        repository_id=repo.id,
                        fix_root_task_id=fixed.root_task_id if fixed else None,
                        head_sha=repo.head_sha,
                        storage_key=repo.storage_key,
                        parser_version=PARSER_VERSION,
                        identity_version=IDENTITY_VERSION,
                        history_coverage="recent_window" if checkpoint.commit_limit else "full",
                        feature_version=version,
                        rule_hash=feature_versions.rule_hash(version),
                        purpose=policy["purpose"],
                        fix_json=frozen,
                        fix_digest=digest(frozen),
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
            raise RepositoryError(409, "FEATURE_STATE_CONFLICT") from None
        except RepositoryError as error:
            if error.code != "FEATURE_STATE_CONFLICT":
                raise
            with Session(self.engine) as db:
                if replay := existing(db):
                    return replay
            raise

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "repository.feature"
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
        point = db.get(FeatureRun, task.root_task_id, with_for_update=True)
        if (
            not repo
            or not point
            or repo.latest_task_id != task.id
            or (repo.feature_root_task_id != task.root_task_id)
        ):
            return None
        if (
            point.head_sha != repo.head_sha
            or point.storage_key != repo.storage_key
            or point.rule_hash != feature_versions.rule_hash(point.feature_version)
            or point.feature_version != task.payload["feature_version"]
            or point.feature_version == feature_versions.V2
            and (point.fix_root_task_id is not None or point.fix_json != {})
            or point.parser_version != PARSER_VERSION
            or point.identity_version != IDENTITY_VERSION
            or digest(point.fix_json) != point.fix_digest
        ):
            raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
        return task, repo, point, now

    def plan(self, task_id, token, reader, head, commit_limit):
        try:
            return self._plan(task_id, token, reader, head)
        except FeatureInputError as error:
            raise RepositoryError(409, error.code) from None

    def _plan(self, task_id, token, reader, head):
        original_tick = reader.tick

        def tick():
            if time.monotonic() > reader.deadline:
                raise RepositoryError(408, "REPOSITORY_PARSE_TIMEOUT")
            original_tick()

        reader.tick = tick
        reachable = set(reader.plan(head, None))
        version = reader.command(["--version"]).decode().strip()
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            point = context[2]
            metadata = self.visible(point)
            is_v2 = point.feature_version == feature_versions.V2
            fixed = point.fix_json
            storage_key = point.storage_key
            if point.git_version and point.git_version != version:
                raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
            items = list(
                db.scalars(
                    select(FeatureItem)
                    .where(FeatureItem.root_task_id == point.root_task_id)
                    .order_by(FeatureItem.ordinal)
                )
            )
            frozen = [row.input_json for row in items]
            input_digest, source_digest = point.input_digest, point.source_digest
        if input_digest is None:
            # Release repository locks before Git/CPU callbacks renew the lease.
            records = []
            with Session(self.engine) as db:
                commits = db.execute(
                    select(GitCommit, AuthorIdentity)
                    .join(AuthorIdentity, GitCommit.author_identity_id == AuthorIdentity.id)
                    .where(
                        GitCommit.repository_id == metadata["repository_id"],
                        GitCommit.parser_version == PARSER_VERSION,
                    )
                ).all()
                eligible = {commit.sha for commit, _ in commits} & reachable
                if is_v2 and len(eligible) > 1000:
                    raise RepositoryError(422, "FEATURE_RESOURCE_LIMIT")
                if metadata["history_coverage"] == "full" and eligible != reachable:
                    raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
                for commit, author in commits:
                    if commit.sha not in eligible:
                        continue
                    reader.tick()
                    records.append(
                        {
                            "commit": {
                                k: getattr(commit, k)
                                for k in (
                                    "sha",
                                    "parents",
                                    "parent_count",
                                    "committer_time",
                                    "parse_status",
                                    "parser_version",
                                )
                            },
                            "identity": {
                                "identity_key": author.identity_key,
                                "identity_version": author.identity_version,
                            },
                            "files": [
                                {
                                    k: getattr(file, k)
                                    for k in (
                                        "old_path",
                                        "new_path",
                                        "old_blob",
                                        "new_blob",
                                        "change_type",
                                        "insertions",
                                        "deletions",
                                        "old_loc",
                                        "content_status",
                                        "is_binary",
                                        "parser_version",
                                    )
                                }
                                for file in db.scalars(
                                    select(FileChange)
                                    .where(FileChange.commit_id == commit.id)
                                    .order_by(FileChange.ordinal)
                                )
                            ],
                        }
                    )
                    if is_v2:
                        records[-1]["commit"].update(
                            {
                                k: getattr(commit, k)
                                for k in (
                                    "message",
                                    "author_time",
                                    "author_offset",
                                    "committer_offset",
                                )
                            }
                        )
                        if sum(len(row["files"]) for row in records) > 10000:
                            raise RepositoryError(422, "FEATURE_RESOURCE_LIMIT")
            inputs = []
            verified = []
            for record in records:
                reader.tick()
                if is_v2:
                    verified.append(feature_versions.freeze_verified(reader, record, encode_input))
                    continue
                signal = fixed.get(record["commit"]["sha"], {}).get("signal")
                fix = None
                if signal:
                    data = dict(signal)
                    data["available_at"] = datetime.fromisoformat(
                        data["available_at"].removesuffix("Z")
                    )
                    fix = FixSignal(**data)
                inputs.append(freeze_native_record(reader, record, fix))
            if is_v2:
                frozen = sorted(verified, key=lambda row: (row["commit"]["event_time"], row["sha"]))
            else:
                frozen = [encode_input(c) for c in sorted(inputs, key=lambda c: c.order)]
        else:
            if digest(frozen) != input_digest:
                raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
            if not is_v2:
                inputs = [decode_input(row) for row in frozen]
        if is_v2:
            inputs, messages = feature_versions.thaw(frozen, decode_input)
        snapshot = Snapshot(
            metadata["repository_id"], head, tuple(inputs), metadata["history_coverage"]
        )
        if is_v2:
            outputs, report = feature_versions.calculate(snapshot, messages, frozen, reader.tick)
        else:
            vectors = compute(snapshot, metadata["purpose"], reader.tick)
            report = quality(vectors, reader.tick)
            outputs = [v.as_dict() for v in vectors]
        # Bind storage/frozen Fix run even when no signal is usable at event time.
        bound = digest(
            {
                "kernel": report["source_digest"],
                "storage_key": storage_key,
                "fix_run": metadata["fix_root_task_id"],
                "fix_digest": metadata["fix_digest"],
                **(
                    {"native_records": report["source_validation"]["native_records_digest"]}
                    if is_v2
                    else {}
                ),
            }
        )
        if source_digest is not None and source_digest != bound:
            raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
        reader.feature_outputs = {v["sha"]: v | {"source_digest": bound} for v in outputs}
        reader.feature_quality = report | {"source_digest": bound}
        self.prepared_quality = reader.feature_quality
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                raise ExecutionStopped
            point = context[2]
            if point.input_digest is None:
                for ordinal, data in enumerate(frozen):
                    db.add(
                        FeatureItem(
                            id=str(uuid4()),
                            root_task_id=point.root_task_id,
                            ordinal=ordinal,
                            sha=data["sha"],
                            input_json=data,
                        )
                    )
                point.input_digest, point.source_digest = digest(frozen), bound
            elif point.input_digest != digest(frozen) or point.source_digest != bound:
                raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
            point.git_version = version
            db.flush()
            if database_now(db) >= context[0].lease_until:
                raise ExecutionStopped
        return [v["sha"] for v in outputs]

    def record(self, task_id, token, reader, sha):
        return reader.feature_outputs[sha]

    def heartbeat(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, _, point, now = context
            task.stage, task.heartbeat_at = "extracting_features", now
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
                raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
            for ordinal, record in enumerate(records, expected):
                item = db.scalar(
                    select(FeatureItem)
                    .where(
                        FeatureItem.root_task_id == point.root_task_id,
                        FeatureItem.ordinal == ordinal,
                    )
                    .with_for_update()
                )
                if (
                    not item
                    or item.sha != record["sha"]
                    or item.result_json is not None
                    or (record["source_digest"] != point.source_digest)
                ):
                    raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
                item.result_json = record
            point.processed = task.processed = end
            if records:
                point.last_sha = records[-1]["sha"]
            task.total, task.heartbeat_at = point.total, now
            task.progress = 100 if complete else min(99, 100 * end / max(point.total, 1))
            task.stage = "extracting_features"
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            if complete:
                # Publish quality only for a complete population; partial batches stay visible
                # as incomplete, never masquerading as a finished quality report.
                db.flush()
                published = db.scalar(
                    select(func.count())
                    .select_from(FeatureItem)
                    .where(
                        FeatureItem.root_task_id == point.root_task_id,
                        FeatureItem.result_json.is_not(None),
                    )
                )
                if (
                    published != end
                    or self.prepared_quality["count"] != end
                    or (self.prepared_quality["source_digest"] != point.source_digest)
                ):
                    raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT")
                point.quality_json = self.prepared_quality
                if database_now(db) >= task.lease_until:
                    raise ExecutionStopped
                task.result_json = {
                    "repository_id": repo.id,
                    "run_id": point.root_task_id,
                    "head_sha": point.head_sha,
                    "source_digest": point.source_digest,
                    "extracted": end,
                    "feature_version": point.feature_version,
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
                "repository_id",
                "fix_root_task_id",
                "head_sha",
                "parser_version",
                "identity_version",
                "history_coverage",
                "feature_version",
                "rule_hash",
                "purpose",
                "fix_digest",
                "source_digest",
                "input_digest",
                "git_version",
                "processed",
                "total",
            )
        } | {"created_at": iso(point.created_at), "complete": point.quality_json is not None}

    def runs(self, repository_id, page=1, page_size=20):
        with Session(self.engine) as db:
            if not db.get(Repository, repository_id):
                raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
            condition = FeatureRun.repository_id == repository_id
            count = db.scalar(select(func.count()).select_from(FeatureRun).where(condition))
            rows = db.scalars(
                select(FeatureRun)
                .where(condition)
                .order_by(FeatureRun.created_at.desc(), FeatureRun.root_task_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "items": [self.visible(row) for row in rows],
                "total": count,
                "page": page,
                "page_size": page_size,
            }

    def results(self, repository_id, run_id, page=1, page_size=20, feature=None, missing=None):
        with Session(self.engine) as db:
            point = db.get(FeatureRun, run_id)
            if not point or point.repository_id != repository_id:
                raise RepositoryError(404, "FEATURE_RUN_NOT_FOUND")
            conditions = [FeatureItem.root_task_id == run_id]
            if feature:
                value = FeatureItem.result_json["values"][feature].as_float()
                conditions += [FeatureItem.result_json.is_not(None)]
                if missing is not None:
                    conditions.append(value.is_(None) if missing else value.is_not(None))
                ordering = (
                    (value.desc(), FeatureItem.ordinal) if not missing else (FeatureItem.ordinal,)
                )
            else:
                ordering = (FeatureItem.ordinal,)
            count = db.scalar(select(func.count()).select_from(FeatureItem).where(*conditions))
            rows = db.scalars(
                select(FeatureItem)
                .where(*conditions)
                .order_by(*ordering)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "run": self.visible(point),
                "quality": point.quality_json,
                "items": [
                    {"sha": row.sha, "ordinal": row.ordinal, "result": row.result_json}
                    for row in rows
                ],
                "total": count,
                "page": page,
                "page_size": page_size,
            }
