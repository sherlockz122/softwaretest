# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from apps.api.application import create_app
from apps.api.auth import current_user
from packages.mining.feature_service import FeatureService
from packages.mining.features import FEATURES, HISTORY
from packages.mining.fix import FixService
from packages.persistence.models import (
    AsyncTask,
    DefectEvidence,
    FeatureItem,
    FeatureRun,
    FixAssessment,
    GitCommit,
    Repository,
    SZZItem,
)
from packages.repositories.parser import ParseExecutor
from packages.repositories.safety import RepositoryError
from packages.repositories.sync import SyncService
from tests.auth_support import migrate, scratch_database
from tests.test_szz import execute, szz_env  # noqa: F401
from tests.test_szz import start as szz_start
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration


def start(env, key=None, purpose="historical"):
    settings, connections, _, actors, repo, fixed, _ = env
    service = FeatureService(settings, connections)
    root = service.create(
        actors[1],
        repo,
        key or str(uuid4()),
        {
            "feature_version": "kamei14-dg-v1",
            "purpose": purpose,
            "fix_run_id": fixed,
        },
        str(uuid4()),
    )["task_id"]
    return service, root


def test_real_features_quality_paging_and_immutable_fix(szz_env):
    settings, connections, _, actors, repo, fixed, shas = szz_env
    service, root = start(szz_env)
    with Session(connections.engine) as db:
        row = db.scalar(
            select(FixAssessment).where(
                FixAssessment.root_task_id == fixed, FixAssessment.sha == shas[1]
            )
        )
        assessment = row.id
    FixService(settings, connections).review(
        actors[1],
        repo,
        assessment,
        {
            "status": "rejected",
            "expected_revision": 0,
            "note": "later review",
        },
        str(uuid4()),
    )
    assert execute(service, settings, root)
    data = service.results(repo, root, page_size=100)
    assert data["total"] == data["quality"]["count"] == 14
    assert data["run"]["complete"] and data["run"]["processed"] == 14
    last = data["items"][-1]["result"]
    assert set(last["values"]) == set(FEATURES)
    assert last["values"]["EXP"] == 13 and last["values"]["NUC"] == 1
    assert last["values"]["LA"] == last["values"]["LD"] == 1
    assert last["values"]["FIX"] is None and last["reasons"]["FIX"] == "fix_not_yet_available"
    assert service.results(repo, root, page=2, page_size=10)["items"][0]["ordinal"] == 10
    assert service.results(repo, root, feature="AGE", missing=True)["total"] == 1
    assert service.results(repo, root, feature="FIX", missing=True)["total"] == 14
    assert service.results(repo, root, feature="EXP", missing=False)["items"][0]["sha"] == shas[-1]
    assert "storage_key" not in str(data) and "@example" not in str(data)
    with Session(connections.engine) as db:
        assert db.get(FeatureRun, root).fix_json[shas[1]]["review_revision"] == 0
        assert db.get(Repository, repo).feature_status == "extracted"
    second, next_root = start(szz_env)
    assert execute(second, settings, next_root)
    assert service.runs(repo)["total"] == 2
    assert service.results(repo, next_root)["run"]["source_digest"] != data["run"]["source_digest"]
    assert service.results(repo, root)["run"]["source_digest"] == data["run"]["source_digest"]


def test_concurrent_idempotency_roles_conflicts_and_validation(szz_env):
    settings, connections, _, actors, repo, fixed, _ = szz_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        roots = list(pool.map(lambda _: start(szz_env, "same-key")[1], range(2)))
    assert roots[0] == roots[1]
    with pytest.raises(RepositoryError, match="TASK_IDEMPOTENCY_CONFLICT"):
        start(szz_env, "same-key", "candidate")
    with pytest.raises(RepositoryError, match="FEATURE_STATE_CONFLICT"):
        start(szz_env)
    with pytest.raises(RepositoryError):
        SyncService(settings, connections).create(actors[1], repo, str(uuid4()), str(uuid4()))
    with pytest.raises(RepositoryError):
        szz_env[2].create(
            actors[1],
            repo,
            str(uuid4()),
            {
                "fix_run_id": fixed,
                "algorithm_version": "baseline-szz-v1",
                "as_of": None,
            },
            str(uuid4()),
        )
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: actors[2]
    with TestClient(app) as client:
        path = f"/api/v1/repositories/{repo}"
        assert (
            client.post(
                path + "/feature-runs", json={}, headers={"Idempotency-Key": "viewer"}
            ).status_code
            == 403
        )
        assert client.get(path + "/feature-runs").status_code == 200
        assert client.get(path + "/feature-quality", params={"run_id": roots[0]}).status_code == 200
        for params in ({"feature": "UNKNOWN"}, {"page_size": 101}, {"page": 0}, {"run_id": "bad"}):
            assert (
                client.get(
                    path + "/feature-quality", params={"run_id": roots[0]} | params
                ).status_code
                == 422
            )
        assert (
            client.get(path + "/feature-quality", params={"run_id": str(uuid4())}).status_code
            == 404
        )
        assert (
            client.post(
                path + "/feature-runs",
                json={"purpose": "magic"},
                headers={"Idempotency-Key": "bad"},
            ).status_code
            == 422
        )


@pytest.mark.parametrize("stop", ["cancel", "lease", "capacity"])
def test_partial_recovery_fencing_and_complete_quality(szz_env, monkeypatch, stop):
    settings, connections, _, actors, repo, *_ = szz_env
    service, root = start(szz_env)
    token, payload = service.tasks.claim(root)
    original = service.batch

    def batch(*args, **kw):
        result = original(*args, **kw)
        if args[2] == 0 and result:
            if stop == "cancel":
                service.tasks.cancel(actors[1], root, str(uuid4()))
            elif stop == "lease":
                expire(connections.engine, "async_task", "lease_until", root)
            else:
                monkeypatch.setattr(service.storage, "usage", lambda _: SimpleNamespace(free=0))
        return result

    monkeypatch.setattr(service, "batch", batch)
    assert not ParseExecutor(settings, service).run(root, token, payload)
    with Session(connections.engine) as db:
        assert db.get(FeatureRun, root).processed == 10
        assert db.get(FeatureRun, root).quality_json is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(FeatureItem)
                .where(FeatureItem.root_task_id == root, FeatureItem.result_json.is_not(None))
            )
            == 10
        )
        source = db.get(FeatureRun, root).source_digest
    monkeypatch.undo()
    if stop == "lease":
        service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root))
        successor = child.id if child else None
    successor = (
        successor or service.tasks.retry(actors[1], root, str(uuid4()), str(uuid4()))["task_id"]
    )
    assert not service.heartbeat(root, token)
    assert execute(service, settings, successor)
    data = service.results(repo, root)
    assert data["quality"]["count"] == 14 and data["run"]["source_digest"] == source


def test_atomic_rollback_and_lost_final_ack(szz_env, monkeypatch):
    settings, connections, *_ = szz_env
    service, root = start(szz_env)
    token, payload = service.tasks.claim(root)
    flush = Session.flush

    def fault(db, *a, **kw):
        if any(
            isinstance(row, FeatureRun) and row.root_task_id == root and row.processed == 14
            for row in db.dirty
        ):
            raise RuntimeError("final batch interrupted")
        return flush(db, *a, **kw)

    monkeypatch.setattr(Session, "flush", fault)
    with pytest.raises(RuntimeError):
        ParseExecutor(settings, service).run(root, token, payload)
    monkeypatch.undo()
    with Session(connections.engine) as db:
        assert db.get(FeatureRun, root).processed == 10
    expire(connections.engine, "async_task", "lease_until", root)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root)).id
    original = service.batch

    def lost(*a, **kw):
        result = original(*a, **kw)
        if kw.get("complete"):
            raise RuntimeError("final ACK lost")
        return result

    monkeypatch.setattr(service, "batch", lost)
    with pytest.raises(RuntimeError):
        execute(service, settings, child)
    with Session(connections.engine) as db:
        assert db.get(AsyncTask, child).status == "succeeded"
        assert db.get(FeatureRun, root).processed == 14


@pytest.mark.parametrize("szz_env", ["recent"], indirect=True)
def test_recent_requires_review_and_explicit_candidate_default(szz_env):
    settings, connections, _, _, repo, *_ = szz_env
    with Session(connections.engine) as db, db.begin():
        db.get(Repository, repo).sync_status = "requires_review"
    service, root = start(szz_env, purpose="candidate")
    assert execute(service, settings, root)
    for row in service.results(repo, root)["items"]:
        assert row["result"]["values"]["FIX"] == 0
        assert all(row["result"]["values"][name] is None for name in HISTORY)
    with Session(connections.engine) as db:
        assert db.get(Repository, repo).sync_status == "requires_review"


def test_frozen_schema_roundtrip_and_whole_path_guard(szz_env):
    service, root = start(szz_env)
    engine = szz_env[1].engine
    for target in ("0007_szz", "base"):
        with pytest.raises(RuntimeError, match="affected tables must be empty"):
            migrate(engine, "downgrade", target)
        with engine.connect() as db:
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "0010_dataset_numbers"
            )
    assert service.results(szz_env[4], root)["total"] == 0
    with scratch_database() as (_, empty):
        migrate(empty)
        migrate(empty, "check")
        migrate(empty, "downgrade", "base")
        migrate(empty)
        migrate(empty, "check")


def test_old_fix_data_preserved_when_feature_schema_downgraded(szz_env):
    engine = szz_env[1].engine
    migrate(engine, "downgrade", "0007_szz")
    migrate(engine)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(FixAssessment)) == 14


def test_unaccepted_database_union_excluded_and_full_gap_rejected(szz_env):
    settings, connections, _, _, repo, _, shas = szz_env
    with Session(connections.engine) as db, db.begin():
        base = db.scalar(select(GitCommit).where(GitCommit.repository_id == repo))
        db.add(
            GitCommit(
                id=str(uuid4()),
                repository_id=repo,
                sha="f" * 40,
                author_identity_id=base.author_identity_id,
                author_time=base.author_time,
                committer_time=base.committer_time,
                author_offset=0,
                committer_offset=0,
                message="unaccepted database row",
                parents=[],
                parent_count=0,
                parse_status="parsed",
                parser_version=base.parser_version,
            )
        )
    service, root = start(szz_env)
    assert execute(service, settings, root)
    assert service.results(repo, root)["total"] == 14
    assert all(row["sha"] != "f" * 40 for row in service.results(repo, root)["items"])
    with Session(connections.engine) as db, db.begin():
        db.scalar(
            select(GitCommit).where(GitCommit.repository_id == repo, GitCommit.sha == shas[0])
        ).parser_version = "obsolete"
    service, root = start(szz_env)
    assert not execute(service, settings, root)
    with Session(connections.engine) as db:
        assert db.get(AsyncTask, root).error_code == "FEATURE_SOURCE_CONFLICT"
        assert db.get(FeatureRun, root).source_digest is None


def test_cpu_cancel_before_input_publication(szz_env, monkeypatch):
    import time

    import packages.mining.feature_service as module

    settings, connections, _, actors, _, *_ = szz_env
    service, root = start(szz_env)
    original = module.compute

    def cancel(snapshot, purpose, tick):
        service.tasks.cancel(actors[1], root, str(uuid4()))
        time.sleep(1.01)  # Real heartbeat interval expires while CPU preparation is active.
        return original(snapshot, purpose, tick)

    token, payload = service.tasks.claim(root)
    monkeypatch.setattr(module, "compute", cancel)
    assert not ParseExecutor(settings, service).run(root, token, payload)
    with Session(connections.engine) as db:
        assert db.get(AsyncTask, root).status == "cancelled"
        assert db.scalar(select(func.count()).select_from(FeatureItem)) == 0


@pytest.mark.parametrize("kind", ["feature", "szz"])
def test_fix_publication_after_request_snapshot_is_frozen_in_current_read(
    szz_env, monkeypatch, kind
):
    settings, connections, _, _, repo, fixed, shas = szz_env
    lookup, published = Event(), Event()
    original = Session.get

    def get(db, entity, *args, **kwargs):
        if entity is Repository:
            lookup.set()
            assert published.wait(10)
        return original(db, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", get)
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    with Session(connections.engine) as source, source.begin():
        row = source.scalar(
            select(FixAssessment).where(
                FixAssessment.root_task_id == fixed, FixAssessment.sha == shas[1]
            )
        )
        source.add(
            DefectEvidence(
                id=str(uuid4()),
                assessment_id=row.id,
                fix_commit_id=row.commit_id,
                ordinal=100,
                type="keyword",
                value="late publication fixture",
                confidence="high",
                rule_version="fix-evidence-v1",
                source={"observed_at": future.isoformat() + "Z"},
            )
        )
        source.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:

            def create_szz():
                return szz_env[2], szz_start(szz_env)

            request = pool.submit(start, szz_env) if kind == "feature" else pool.submit(create_szz)
            assert lookup.wait(10)  # existing() already established the older RR snapshot.
            source.commit()
            published.set()
            service, root = request.result(timeout=15)
    with Session(connections.engine) as db:
        signal = (
            db.get(FeatureRun, root).fix_json[shas[1]]["signal"]
            if kind == "feature"
            else db.scalar(
                select(SZZItem).where(SZZItem.root_task_id == root, SZZItem.sha == shas[1])
            ).input_json
        )
        assert signal["available_at"] == future.isoformat(timespec="microseconds") + "Z"
    assert execute(service, settings, root)
