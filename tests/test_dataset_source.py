# ruff: noqa: F811
"""Real Git/MySQL source adapter. The fixture's accepting task is synthetic."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.mining.dataset import DatasetError
from packages.mining.dataset_source import DatasetSource
from packages.mining.fix import FixService
from packages.persistence.models import (
    AsyncTask,
    Base,
    FeatureItem,
    FixAssessment,
    Repository,
    SZZRun,
)
from packages.repositories.safety import RepositoryError
from tests.test_feature_service import start as feature_start
from tests.test_szz import execute, szz_env  # noqa: F401
from tests.test_szz import start as szz_start
from tests.test_tasks import task_env  # noqa: F401

pytestmark = pytest.mark.integration


def sources(env):
    settings, connections, service, _, repo, _, _ = env
    feature_service, feature = feature_start(env)
    assert execute(feature_service, settings, feature)
    szz = szz_start(env)
    assert execute(service, settings, szz)
    with Session(connections.engine) as db, db.begin():
        row = db.get(Repository, repo)
        clone = db.scalar(select(AsyncTask).where(AsyncTask.type == "repository.clone"))
        clone.started_at = datetime.utcnow() - timedelta(minutes=1)
        clone.finished_at = datetime.utcnow() - timedelta(seconds=50)
        clone.execution_token = row.storage_key.split("/")[-2]
        clone.result_json = {"repository_id": repo, "head_sha": row.head_sha}
    return DatasetSource(settings, connections), feature, szz


def counts(engine):
    with Session(engine) as db:
        return {
            name: db.scalar(select(func.count()).select_from(table))
            for name, table in Base.metadata.tables.items()
        }


def test_current_real_git_rounds_are_read_only_and_not_ready(szz_env):
    source, feature, szz = sources(szz_env)
    before = counts(source.engine)
    loaded = source.load(szz_env[4], feature, [szz])
    result = source.audit(szz_env[4], feature, [szz])
    assert len(loaded[0].commits) == len(loaded[1]) == len(loaded[2][0].records) == 14
    assert loaded[2][0].observed_at < loaded[2][0].published_at
    assert not result["ready"] and result["label_counts"]["unknown"] >= 13
    assert result["source_digest"] and result["unknown_reasons"]["unresolved_evidence"]
    assert counts(source.engine) == before


@pytest.mark.parametrize(
    "variant", ["digest", "quality", "unaccepted", "candidate", "incomplete", "no_accepting_time"]
)
def test_invalid_source_and_missing_coverage_proof_rejected(szz_env, variant):
    source, feature, szz = sources(szz_env)
    with Session(source.engine) as db, db.begin():
        if variant == "digest":
            row = db.scalar(select(FeatureItem).where(FeatureItem.root_task_id == feature))
            row.input_json = row.input_json | {"identity_key": "0" * 64}
        elif variant == "quality":
            from packages.persistence.models import FeatureRun

            row = db.get(FeatureRun, feature)
            row.quality_json = row.quality_json | {"count": 0}
        elif variant == "unaccepted":
            db.get(Repository, szz_env[4]).sync_status = "requires_review"
        elif variant == "candidate":
            from packages.persistence.models import FeatureRun

            db.get(FeatureRun, feature).purpose = "candidate"
        elif variant == "incomplete":
            db.get(SZZRun, szz).processed = 0
        else:
            db.scalar(
                select(AsyncTask).where(AsyncTask.type == "repository.clone")
            ).started_at = None
    with pytest.raises(DatasetError):
        source.load(szz_env[4], feature, [szz])


def test_review_change_becomes_conflict_without_rewriting_sources(szz_env):
    source, feature, szz = sources(szz_env)
    with Session(source.engine) as db:
        row = db.scalar(
            select(FixAssessment).where(
                FixAssessment.root_task_id == szz_env[5], FixAssessment.sha == szz_env[6][1]
            )
        )
        assessment = row.id
    FixService(szz_env[0], szz_env[1]).review(
        szz_env[3][1],
        szz_env[4],
        assessment,
        {"status": "rejected", "expected_revision": 0, "note": "fixture changed review"},
        str(uuid4()),
    )
    loaded = source.load(szz_env[4], feature, [szz])
    changed = next(e for e in loaded[2][0].records if e.sha == szz_env[6][1])
    assert not changed.current_review_matches
    assert source.audit(szz_env[4], feature, [szz])["labels"][0]["reason"] == "review_conflict"


def test_capacity_rejects_before_read(szz_env):
    source, feature, szz = sources(szz_env)

    class Low:
        def capacity(self, growth):
            assert growth == 256 * 1024**2
            raise RepositoryError(507, "REPOSITORY_STORAGE_LOW")

    source.storage = Low()
    with pytest.raises(RepositoryError, match="REPOSITORY_STORAGE_LOW"):
        source.load(szz_env[4], feature, [szz])


def test_future_snapshot_and_duplicate_round_selection(szz_env):
    source, feature, szz = sources(szz_env)
    with pytest.raises(DatasetError, match="DATA_SNAPSHOT_IN_FUTURE"):
        source.load(szz_env[4], feature, [szz], datetime.utcnow() + timedelta(days=1))
    with pytest.raises(DatasetError, match="DATA_OBSERVATION_SELECTION_INVALID"):
        source.load(szz_env[4], feature, [szz, szz])


def test_input_size_and_deadline_reject_read_only(szz_env, monkeypatch):
    source, feature, szz = sources(szz_env)
    before = counts(source.engine)
    monkeypatch.setattr("packages.mining.dataset_source.LIMIT", 0)
    with pytest.raises(DatasetError, match="DATA_AUDIT_INPUT_LIMIT"):
        source.load(szz_env[4], feature, [szz])
    assert counts(source.engine) == before
    monkeypatch.setattr("packages.mining.dataset_source.LIMIT", 32 * 1024**2)
    stamps = iter([0, 31])
    monkeypatch.setattr("packages.mining.dataset_source.time.monotonic", lambda: next(stamps))
    with pytest.raises(DatasetError, match="DATA_AUDIT_TIMEOUT"):
        source.load(szz_env[4], feature, [szz])
