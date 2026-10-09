from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from apps.api.application import create_app
from apps.api.auth import current_user
from packages.mining.fix import FixService
from packages.mining.fix_rules import RULE_VERSION
from packages.mining.szz import SZZService
from packages.persistence.models import (
    AsyncTask,
    FixAssessment,
    Repository,
    SZZItem,
    SZZRun,
)
from packages.repositories.parser import ParseExecutor
from packages.repositories.parsing import ParsingService
from packages.repositories.safety import RepositoryError
from packages.repositories.storage import GIB, Storage
from packages.repositories.sync import SyncService
from tests.auth_support import migrate, scratch_database
from tests.repository_support import git
from tests.szz_support import szz_history
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration


def execute(service, settings, task_id):
    token, payload = service.tasks.claim(task_id)
    return ParseExecutor(settings, service).run(task_id, token, payload)


@pytest.fixture
def szz_env(task_env, tmp_path, request):  # noqa: F811
    settings, connections, tasks, actors = task_env
    settings = settings.model_copy(update={"repository_storage_root": tmp_path / "repositories"})
    source, _, _, shas = szz_history(
        tmp_path / "source", count=12, empty=getattr(request, "param", None) == "empty"
    )
    repo_id = str(uuid4())
    attempt = Storage(settings).attempt(repo_id, str(uuid4()))
    bare = attempt / "repo.git"
    git("clone", "--bare", str(source), str(bare))
    with Session(connections.engine) as db, db.begin():
        task = tasks.new_task(
            db,
            actors[1].id,
            str(uuid4()),
            {"repository_id": repo_id},
            "fixture",
            str(uuid4()),
            kind="repository.clone",
        )
        task.status = task.stage = "succeeded"
        db.add(
            Repository(
                id=repo_id,
                owner_id=actors[1].id,
                canonical_url="https://fixture.example/szz/demo.git",
                status="cloned",
                latest_task_id=task.id,
                head_sha=shas[-1] if shas else None,
                default_branch="main",
                storage_key=bare.relative_to(settings.repository_storage_root).as_posix(),
                size_bytes=1,
            )
        )
    parser = ParsingService(settings, connections)
    limit = 2 if getattr(request, "param", None) == "recent" else None
    parsing = parser.create(actors[1], repo_id, str(uuid4()), limit, str(uuid4()))["task_id"]
    assert execute(parser, settings, parsing)
    fix = FixService(settings, connections)
    fixed = fix.create(
        actors[1],
        repo_id,
        str(uuid4()),
        {"rule_version": RULE_VERSION, "include_medium": True},
        str(uuid4()),
    )["task_id"]
    assert execute(fix, settings, fixed)
    yield settings, connections, SZZService(settings, connections), actors, repo_id, fixed, shas


def start(env, key=None, cutoff=None):
    return env[2].create(
        env[3][1],
        env[4],
        key or str(uuid4()),
        {"fix_run_id": env[5], "algorithm_version": "baseline-szz-v1", "as_of": cutoff},
        str(uuid4()),
    )["task_id"]


def test_real_szz_run_links_freeze_and_recompute(szz_env):
    settings, connections, service, actors, repo, fixed, shas = szz_env
    root = start(szz_env)
    with Session(connections.engine) as db:
        row = db.scalar(
            select(FixAssessment).where(
                FixAssessment.root_task_id == fixed, FixAssessment.sha == shas[1]
            )
        )
        assessment_id = row.id
    FixService(settings, connections).review(
        actors[1],
        repo,
        assessment_id,
        {"status": "rejected", "expected_revision": 0, "note": "after SZZ input freeze"},
        str(uuid4()),
    )
    assert execute(service, settings, root)
    links = service.links(root, page_size=100)
    assert links["total"] == 13 and all(row["eligible"] for row in links["items"])
    link = next(row for row in links["items"] if row["fix_sha"] == shas[1])
    assert link["blamed_sha"] == shas[0] and link["fixed_line"] == link["blamed_line"] == 1
    assert link["confidence"] == "medium" and link["label_available_at"] <= links["run"]["as_of"]
    assert "storage_key" not in str(links) and "@example" not in str(links)
    results = service.results(root, page_size=100)
    original = next(r for r in results["items"] if r["sha"] == shas[1])
    assert original["input"]["review_revision"] == 0 and original["result"]["status"] == "traced"
    second = start(szz_env)
    assert execute(service, settings, second)
    assert service.links(second)["total"] == 12 and service.links(root)["total"] == 13
    assert (
        service.runs(repo)["total"] == 2
        and service.results(second)["run"]["label_version"] != results["run"]["label_version"]
    )
    assert len(service.links(root, page=2, page_size=10)["items"]) == 3
    with Session(connections.engine) as db:
        assert db.get(Repository, repo).szz_status == "traced"


def test_creation_concurrency_idempotency_api_roles_bounds(szz_env):
    settings, connections, service, actors, repo, fixed, _ = szz_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        roots = list(pool.map(lambda _: start(szz_env, "same"), range(2)))
    assert roots[0] == roots[1]
    with pytest.raises(RepositoryError):
        start(szz_env, "same", "2020-01-01T00:00:00Z")
    with pytest.raises(RepositoryError):
        start(szz_env)
    with pytest.raises(RepositoryError):
        SyncService(settings, connections).create(actors[1], repo, str(uuid4()), str(uuid4()))
    with pytest.raises(RepositoryError):
        FixService(settings, connections).create(
            actors[1],
            repo,
            str(uuid4()),
            {"rule_version": RULE_VERSION, "include_medium": True},
            str(uuid4()),
        )
    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: actors[2]
    with TestClient(app) as client:
        path = "/api/v1/repositories/" + repo + "/szz-runs"
        assert (
            client.post(
                path, json={"fix_run_id": fixed}, headers={"Idempotency-Key": str(uuid4())}
            ).status_code
            == 403
        )
        assert client.get(path).status_code == 200
        assert client.get(f"/api/v1/szz-runs/{roots[0]}/links?page_size=101").status_code == 422
        assert client.get(f"/api/v1/szz-runs/{uuid4()}/results").status_code == 404
        app.dependency_overrides[current_user] = lambda: actors[1]
        for extra in [
            {"as_of": "2020-01-01"},
            {"algorithm_version": "unknown"},
            {"confirmed_bug": True},
        ]:
            assert (
                client.post(
                    path,
                    json={"fix_run_id": fixed, **extra},
                    headers={"Idempotency-Key": str(uuid4())},
                ).status_code
                == 422
            )


@pytest.mark.parametrize("cutoff", ["2000-01-01T00:00:00Z", "future"])
def test_observation_time_and_future_rejection(szz_env, cutoff):
    if cutoff == "future":
        with pytest.raises(RepositoryError) as error:
            start(szz_env, cutoff=(datetime.now(UTC) + timedelta(days=1)).isoformat())
        assert error.value.code == "SZZ_CUTOFF_FUTURE"
        return
    root = start(szz_env, cutoff=cutoff)
    assert execute(szz_env[2], szz_env[0], root)
    assert szz_env[2].links(root)["total"] == 0
    assert all(
        row["result"]["status"] == "unknown"
        for row in szz_env[2].results(root, page_size=100)["items"]
    )


@pytest.mark.parametrize("stop", ["cancel", "lease", "capacity"])
def test_partial_batch_cancel_lease_capacity_and_successor(szz_env, monkeypatch, stop):
    settings, connections, service, actors, repo, _, _ = szz_env
    root = start(szz_env)
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
        assert db.get(SZZRun, root).processed == 10
        assert (
            db.scalar(
                select(func.count())
                .select_from(SZZItem)
                .where(SZZItem.root_task_id == root, SZZItem.result_json.is_not(None))
            )
            == 10
        )
    monkeypatch.undo()
    if stop == "lease":
        service.tasks.reconcile()
    with Session(connections.engine) as db:
        parent = db.get(AsyncTask, root)
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root))
        assert parent.status in {"failed", "cancelled"}
    retry = (
        child.id
        if child
        else service.tasks.retry(actors[1], root, str(uuid4()), str(uuid4()))["task_id"]
    )
    assert not service.heartbeat(root, token)
    assert execute(service, settings, retry)
    assert service.links(root)["total"] == 13


def test_atomic_link_rollback_and_lost_final_ack(szz_env, monkeypatch):
    settings, connections, service, actors, *_ = szz_env
    root = start(szz_env)
    token, payload = service.tasks.claim(root)
    flush = Session.flush

    def fault(db, *a, **kw):
        if any(
            isinstance(point, SZZRun) and point.root_task_id == root and point.processed == 14
            for point in db.dirty
        ):
            raise RuntimeError("second batch insertion interrupted")
        return flush(db, *a, **kw)

    monkeypatch.setattr(Session, "flush", fault)
    with pytest.raises(RuntimeError):
        ParseExecutor(settings, service).run(root, token, payload)
    monkeypatch.undo()
    with Session(connections.engine) as db:
        assert db.get(SZZRun, root).processed == 10
    expire(connections.engine, "async_task", "lease_until", root)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root)).id
    original = service.batch

    def lost(*a, **kw):
        result = original(*a, **kw)
        if kw.get("complete"):
            raise RuntimeError("final acknowledgement interrupted")
        return result

    monkeypatch.setattr(service, "batch", lost)
    with pytest.raises(RuntimeError):
        execute(service, settings, child)
    with Session(connections.engine) as db:
        assert (
            db.get(AsyncTask, child).status == "succeeded" and db.get(SZZRun, root).processed == 14
        )
    assert service.links(root)["total"] == 13


@pytest.mark.parametrize("szz_env", ["recent"], indirect=True)
def test_recent_history_unknown_origin_and_requires_review(szz_env):
    settings, connections, service, _, repo, _, _ = szz_env
    with Session(connections.engine) as db, db.begin():
        db.get(Repository, repo).sync_status = "requires_review"
    root = start(szz_env)
    assert execute(service, settings, root)
    links = service.links(root)
    assert links["total"] == 2 and sum(r["eligible"] for r in links["items"]) == 1
    assert any(r["reason"] == "outside_parsed_history" for r in links["items"])
    assert links["run"]["history_coverage"] == "recent_window"
    with Session(connections.engine) as db:
        assert db.get(Repository, repo).sync_status == "requires_review"


def test_capacity_before_acceptance_version_and_source_guards(szz_env, monkeypatch):
    settings, connections, service, _, _, _, _ = szz_env
    monkeypatch.setattr(service.storage, "usage", lambda _: SimpleNamespace(free=GIB))
    with pytest.raises(RepositoryError):
        start(szz_env)
    with Session(connections.engine) as db:
        assert db.scalar(select(func.count()).select_from(SZZRun)) == 0
    monkeypatch.undo()
    root = start(szz_env)
    with Session(connections.engine) as db, db.begin():
        db.get(SZZRun, root).algorithm_hash = "f" * 64
    assert not execute(service, settings, root)
    with Session(connections.engine) as db:
        assert db.get(AsyncTask, root).error_code == "SZZ_VERSION_CONFLICT"


def test_frozen_migration_empty_roundtrip_and_populated_path_guard(szz_env):
    root = start(szz_env)
    with pytest.raises(RuntimeError, match="affected tables must be empty"):
        migrate(szz_env[1].engine, "downgrade", "base")
    with szz_env[1].engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0007_szz"
    assert szz_env[2].results(root)["total"] == 14
    with scratch_database() as (_, engine):
        migrate(engine)
        migrate(engine, "check")
        migrate(engine, "downgrade", "base")
        migrate(engine)
        migrate(engine, "check")


@pytest.mark.parametrize("szz_env", ["empty"], indirect=True)
def test_empty_accepted_history(szz_env):
    root = start(szz_env)
    assert execute(szz_env[2], szz_env[0], root)
    assert szz_env[2].results(root)["total"] == szz_env[2].links(root)["total"] == 0
    assert szz_env[2].results(root)["run"]["processed"] == 0


def test_old_fix_data_preserved_through_0006_upgrade(szz_env):
    engine = szz_env[1].engine
    migrate(engine, "downgrade", "0006_fix_evidence")
    with engine.connect() as db:
        assert db.execute(text("SELECT COUNT(*) FROM fix_assessment")).scalar() == 14
        assert db.execute(text("SELECT COUNT(*) FROM git_commit")).scalar() == 14
    migrate(engine)
    migrate(engine, "check")
    root = start(szz_env)
    assert execute(szz_env[2], szz_env[0], root)
    assert szz_env[2].links(root)["total"] == 13
