"""Read-only current-locking snapshot of complete accepted feature/SZZ rounds."""

import json
import math
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.mining.dataset import (
    WINDOW_DAYS,
    DatasetError,
    Evidence,
    Link,
    Observation,
    Policy,
    audit,
)
from packages.mining.feature_service import decode_input
from packages.mining.features import (
    FEATURE_VERSION,
    FEATURES,
    RULE_HASH,
    FeatureInputError,
    FeatureVector,
    Snapshot,
    digest,
    quality,
)
from packages.mining.szz import digest as szz_digest
from packages.mining.szz_engine import ALGORITHM_HASH, ALGORITHM_VERSION
from packages.persistence.models import (
    AsyncTask,
    DefectEvidence,
    FeatureItem,
    FeatureRun,
    FixAssessment,
    FixRun,
    Repository,
    SyncWindow,
    SZZItem,
    SZZLink,
    SZZRun,
)
from packages.repositories.storage import Storage
from packages.tasks.service import database_now

LIMIT = 32 * 1024**2


def timestamp(value):
    return value.astimezone(UTC).replace(tzinfo=None)


def quality_matches(actual, stored):
    """MySQL JSON can round floating aggregates; counts and structure stay exact."""
    if isinstance(actual, dict) and isinstance(stored, dict):
        return actual.keys() == stored.keys() and all(
            quality_matches(value, stored[key]) for key, value in actual.items()
        )
    if type(actual) is float and type(stored) is float:
        return math.isclose(actual, stored, rel_tol=1e-12, abs_tol=1e-12)
    return type(actual) is type(stored) and actual == stored


class DatasetSource:
    def __init__(self, settings, connections, storage=None):
        self.settings, self.engine = settings, connections.engine
        self.storage = storage or Storage(settings)

    def load(self, *args, **kwargs):
        try:
            return self._load(*args, **kwargs)
        except DatasetError:
            raise
        except (FeatureInputError, KeyError, TypeError, ValueError, IndexError):
            raise DatasetError("DATA_SOURCE_CONFLICT") from None

    def _load(
        self, repository_id, feature_run_id, szz_run_ids, snapshot_at=None, window_days=WINDOW_DAYS
    ):
        # No table update, audit insert, Git command, remote lookup or historical repair.
        if not 1 <= len(szz_run_ids) <= 32 or len(set(szz_run_ids)) != len(szz_run_ids):
            raise DatasetError("DATA_OBSERVATION_SELECTION_INVALID")
        deadline, consumed = time.monotonic() + 30, 0

        def tick():
            self.storage.capacity(256 * 1024**2)
            if time.monotonic() >= deadline:
                raise DatasetError("DATA_AUDIT_TIMEOUT")

        def account(value):
            nonlocal consumed
            consumed += len(json.dumps(value, default=str).encode())
            if consumed > LIMIT:
                raise DatasetError("DATA_AUDIT_INPUT_LIMIT")
            tick()

        def failed():
            raise DatasetError("DATA_SOURCE_CONFLICT")

        with Session(self.engine) as db, db.begin():
            tick()
            now = database_now(db)
            if snapshot_at is not None and snapshot_at > now:
                raise DatasetError("DATA_SNAPSHOT_IN_FUTURE")
            policy = Policy(snapshot_at or now, window_days)
            repo = db.get(Repository, repository_id, with_for_update={"read": True})
            feature = db.get(FeatureRun, feature_run_id, with_for_update={"read": True})
            if (
                not repo
                or not feature
                or feature.repository_id != repository_id
                or repo.status != "cloned"
                or repo.parse_status != "parsed"
                or repo.sync_status not in ("pending", "synced")
                or any(
                    getattr(repo, name)
                    in ("queued", "extracting", "detecting", "tracing", "syncing")
                    for name in ("feature_status", "fix_status", "szz_status", "sync_status")
                )
                or feature.head_sha != repo.head_sha
                or feature.storage_key != repo.storage_key
                or feature.total is None
                or feature.total != feature.processed
                or feature.quality_json is None
                or feature.rule_hash != RULE_HASH
                or feature.feature_version != FEATURE_VERSION
                or feature.purpose != "historical"
            ):
                failed()
            items = db.scalars(
                select(FeatureItem)
                .where(FeatureItem.root_task_id == feature_run_id)
                .order_by(FeatureItem.ordinal)
                .with_for_update(read=True)
            ).all()
            if (
                len(items) != feature.total
                or [item.ordinal for item in items] != list(range(len(items)))
                or any(item.result_json is None for item in items)
            ):
                failed()
            for item in items:
                account(item.input_json)
                account(item.result_json)
            frozen = [item.input_json for item in items]
            if (
                digest(frozen) != feature.input_digest
                or digest(feature.fix_json) != feature.fix_digest
            ):
                failed()
            snapshot = Snapshot(
                repo.id,
                repo.head_sha,
                tuple(decode_input(x) for x in frozen),
                feature.history_coverage,
                feature.parser_version,
                feature.identity_version,
            )
            vectors = []
            for item in items:
                data = item.result_json
                if (
                    data["sha"] != item.sha
                    or data["source_digest"] != feature.source_digest
                    or timestamp(datetime.fromisoformat(data["event_time"]))
                    != snapshot.commits[item.ordinal].event_time
                ):
                    failed()
                vectors.append(
                    FeatureVector(
                        data["sha"],
                        snapshot.commits[item.ordinal].event_time,
                        data["source_digest"],
                        tuple((name, data["values"][name]) for name in FEATURES),
                        tuple(sorted(data["reasons"].items())),
                        data["feature_version"],
                    )
                )

            if not quality_matches(quality(tuple(vectors), tick), feature.quality_json):
                failed()

            def succeeded(root):
                tasks = db.scalars(
                    select(AsyncTask)
                    .where(AsyncTask.root_task_id == root, AsyncTask.status == "succeeded")
                    .with_for_update(read=True)
                ).all()
                if not tasks or any(task.finished_at is None for task in tasks):
                    failed()
                return max(task.finished_at for task in tasks)

            succeeded(feature_run_id)
            observations = []
            for root in sorted(szz_run_ids):
                tick()
                point = db.get(SZZRun, root, with_for_update={"read": True})
                if (
                    not point
                    or point.repository_id != repository_id
                    or point.total is None
                    or point.total != point.processed
                    or point.algorithm_version != ALGORITHM_VERSION
                    or point.algorithm_hash != ALGORITHM_HASH
                    or point.parser_version != feature.parser_version
                ):
                    failed()
                fixed = db.get(FixRun, point.fix_root_task_id, with_for_update={"read": True})
                if (
                    not fixed
                    or fixed.repository_id != repo.id
                    or fixed.head_sha != point.head_sha
                    or fixed.storage_key != point.storage_key
                    or fixed.total != point.total
                    or fixed.processed != fixed.total
                ):
                    failed()
                rows = db.scalars(
                    select(SZZItem)
                    .where(SZZItem.root_task_id == root)
                    .order_by(SZZItem.sha)
                    .with_for_update(read=True)
                ).all()
                if len(rows) != point.total or any(row.result_json is None for row in rows):
                    failed()
                if szz_digest([row.input_json for row in rows]) != point.source_digest:
                    failed()
                # Earlier accepted private snapshots are retained. Match the physical
                # snapshot to its actual accepting clone/sync, never SZZ start time.
                windows = db.scalars(
                    select(SyncWindow)
                    .where(
                        SyncWindow.repository_id == repository_id,
                        SyncWindow.head_sha == point.head_sha,
                        SyncWindow.storage_key == point.storage_key,
                        SyncWindow.relation.in_(["initial", "unchanged", "fast_forward"]),
                    )
                    .with_for_update(read=True)
                ).all()
                observed = []
                for window in windows:
                    tasks = db.scalars(
                        select(AsyncTask)
                        .where(
                            AsyncTask.root_task_id == window.root_task_id,
                            AsyncTask.status == "succeeded",
                        )
                        .with_for_update(read=True)
                    ).all()
                    observed += [t.started_at for t in tasks if t.started_at and t.finished_at]
                if not windows:
                    clones = db.scalars(
                        select(AsyncTask)
                        .where(
                            AsyncTask.type == "repository.clone", AsyncTask.status == "succeeded"
                        )
                        .with_for_update(read=True)
                    ).all()
                    observed += [
                        t.started_at
                        for t in clones
                        if t.started_at
                        and t.finished_at
                        and t.result_json
                        and t.result_json.get("repository_id") == repo.id
                        and t.result_json.get("head_sha") == point.head_sha
                        and point.storage_key == f"objects/{repo.id}/{t.execution_token}/repo.git"
                    ]
                if not observed:
                    raise DatasetError("DATA_OBSERVATION_TIME_UNVERIFIED")
                published = max(succeeded(root), succeeded(fixed.root_task_id))
                records, outputs = [], []
                for row in rows:
                    account(row.input_json)
                    account(row.result_json)
                    data = row.input_json
                    assessment = db.get(
                        FixAssessment, row.assessment_id, with_for_update={"read": True}
                    )
                    if (
                        not assessment
                        or assessment.root_task_id != fixed.root_task_id
                        or assessment.sha != row.sha
                    ):
                        failed()
                    evidence = db.scalars(
                        select(DefectEvidence)
                        .where(DefectEvidence.assessment_id == row.assessment_id)
                        .order_by(DefectEvidence.ordinal)
                        .with_for_update(read=True)
                    ).all()
                    proof = db.scalars(
                        select(SZZLink)
                        .where(SZZLink.item_id == row.id)
                        .order_by(SZZLink.ordinal)
                        .with_for_update(read=True)
                    ).all()
                    if [p.ordinal for p in proof] != list(range(len(proof))) or any(
                        p.evidence["fix_sha"] != row.sha
                        or p.evidence["label_available_at"] != data["available_at"]
                        for p in proof
                    ):
                        failed()
                    evidence_digest = szz_digest(
                        [
                            dict(
                                type=e.type, value=e.value, confidence=e.confidence, source=e.source
                            )
                            for e in evidence
                        ]
                    )
                    matches = (
                        assessment.review_revision == data["review_revision"]
                        and assessment.review_status == data["review_status"]
                        and evidence_digest == data["evidence_digest"]
                    )
                    complete = all(
                        e.type != "unavailable"
                        and (
                            e.type not in ("issue_reference", "bug_issue")
                            or e.source.get("status") in ("bug", "not_bug", "pull_request")
                        )
                        for e in evidence
                    )
                    outputs.append(
                        dict(
                            sha=row.sha,
                            result=row.result_json,
                            links=[p.evidence for p in proof],
                            content_complete=assessment.content_complete,
                            evidence_digest=evidence_digest,
                        )
                    )
                    records.append(
                        Evidence(
                            row.sha,
                            timestamp(datetime.fromisoformat(data["available_at"])),
                            data["candidate"],
                            data["review_status"],
                            data["review_revision"],
                            matches,
                            assessment.content_complete,
                            complete,
                            row.result_json["status"],
                            tuple(
                                Link(e["blamed_sha"], e["eligible"])
                                for e in [p.evidence for p in proof]
                            ),
                        )
                    )
                observations.append(
                    Observation(
                        root,
                        point.head_sha,
                        min(observed),
                        published,
                        point.as_of,
                        digest(
                            dict(
                                szz_source=point.source_digest,
                                algorithm_hash=point.algorithm_hash,
                                label_version=point.label_version,
                                git_version=point.git_version,
                                fix_rule_hash=fixed.rule_hash,
                                storage_key=point.storage_key,
                                outputs=outputs,
                            )
                        ),
                        tuple(records),
                        point.history_coverage,
                        point.parser_version,
                    )
                )
            tick()
            return snapshot, tuple(vectors), tuple(observations), policy

    def audit(self, *args, **kwargs):
        source = self.load(*args, **kwargs)
        deadline = time.monotonic() + 30

        def tick():
            self.storage.capacity(256 * 1024**2)
            if time.monotonic() >= deadline:
                raise DatasetError("DATA_AUDIT_TIMEOUT")

        return audit(*source, tick=tick)
