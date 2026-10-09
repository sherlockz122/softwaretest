from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.api.application import create_app
from apps.api.auth import current_user
from packages.persistence.models import SCHEMA_HEAD, AsyncTask, OperationLog, Repository, TaskOutbox
from packages.repositories.clone import CloneExecutor
from packages.repositories.safety import RepositoryError
from packages.repositories.service import RepositoryService
from packages.repositories.storage import GIB, Storage
from packages.tasks.service import TaskError
from tests.auth_support import migrate
from tests.repository_support import git, https_git
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def repo_env(task_env, tmp_path):  # noqa: F811
    settings, connections, _, actors = task_env
    root = tmp_path / "repos"
    root.mkdir()
    settings = settings.model_copy(update={"repository_storage_root": root})
    with https_git(tmp_path / "tls") as fixture:
        service = RepositoryService(settings, connections, probe=fixture["probe"])
        yield settings, connections, service, actors, fixture


def create(env, key=None):
    return env[2].create(
        env[3][1], key or str(uuid4()), "https://repo.example/team/demo", str(uuid4())
    )


def executor(env):
    settings, _, service, _, fixture = env
    return CloneExecutor(
        settings, service, fixture["policy"], fixture["connector"], cafile=fixture["certfile"]
    )


def test_concurrent_repository_acceptance_and_database_constraints(repo_env):
    settings, connections, service, actors, fixture = repo_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(repo_env, "same-key"), range(2)))
    assert results[0] == results[1]
    with pytest.raises(TaskError) as error:
        service.create(actors[1], "same-key", "https://repo.example/team/other", str(uuid4()))
    assert error.value.code == "TASK_IDEMPOTENCY_CONFLICT"
    with pytest.raises(RepositoryError) as error:
        create(repo_env)
    assert error.value.code == "REPOSITORY_ALREADY_EXISTS"
    with Session(connections.engine) as db:
        for model in (Repository, AsyncTask, TaskOutbox, OperationLog):
            assert db.scalar(select(func.count()).select_from(model)) == 1
    for column, value in (
        ("status", "invalid"),
        ("size_bytes", -1),
        ("latest_task_id", str(uuid4())),
        ("owner_id", str(uuid4())),
    ):
        with pytest.raises(DBAPIError), connections.engine.begin() as db:
            db.execute(text(f"UPDATE repository SET {column}=:v"), {"v": value})
    with pytest.raises(RuntimeError, match="Downgrade refused"):
        migrate(connections.engine, "downgrade", "base")
    with connections.engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == SCHEMA_HEAD
    migrate(connections.engine, "check")


def test_real_native_git_clone_publication_and_duplicate_delivery(repo_env):
    settings, connections, service, actors, fixture = repo_env
    result = create(repo_env)
    token, payload = service.tasks.claim(result["task_id"])
    assert not service.tasks.claim(result["task_id"])
    assert executor(repo_env).run(result["task_id"], token, payload)
    visible = service.detail(result["repository_id"])
    assert visible["status"] == "cloned" and not visible["commits_imported"]
    assert visible["head_sha"] == fixture["head"] and visible["default_branch"] == "main"
    assert visible["size_bytes"] > 0 and visible["task"]["status"] == "succeeded"
    assert "storage_key" not in visible
    with Session(connections.engine) as db:
        repo = db.get(Repository, result["repository_id"])
        bare = settings.repository_storage_root / repo.storage_key
    assert git("-C", str(bare), "rev-parse", "HEAD").decode().strip() == fixture["head"]
    assert git("-C", str(bare), "rev-parse", "--is-bare-repository").strip() == b"true"
    assert not service.publish(result["task_id"], token, {}, "wrong")
    assert bare.exists()


def test_rejected_network_and_audit_failure_leave_no_records(repo_env, monkeypatch):
    _, connections, service, _, fixture = repo_env
    fixture["state"]["redirect"] = True
    with pytest.raises(RepositoryError):
        create(repo_env)
    fixture["state"]["redirect"] = False

    def fail(*args):
        raise RuntimeError("audit failure")

    monkeypatch.setattr("packages.tasks.service.audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        create(repo_env)
    with Session(connections.engine) as db:
        for model in (Repository, AsyncTask, TaskOutbox, OperationLog):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_api_roles_strict_body_errors_and_read_only_catalog(repo_env, monkeypatch):
    settings, connections, service, actors, fixture = repo_env
    monkeypatch.setattr("apps.api.repositories.service", lambda _: service)
    app = create_app(settings)
    identity = [actors[2]]
    app.dependency_overrides[current_user] = lambda: identity[0]
    with TestClient(app) as api:
        response = api.post(
            "/api/v1/repositories",
            json={"url": "https://repo.example/team/demo"},
            headers={"Idempotency-Key": "api-key"},
        )
        assert response.status_code == 403
        identity[0] = actors[1]
        for value in (
            {"url": "https://user:private@github.com/a/b"},
            {"url": "https://repo.example/team/demo", "commit_limit": 10},
        ):
            response = api.post(
                "/api/v1/repositories", json=value, headers={"Idempotency-Key": "api-key"}
            )
            assert response.status_code == 422
            assert "private" not in response.text and "request_id" in response.json()
        response = api.post(
            "/api/v1/repositories",
            json={"url": "https://repo.example/team/demo"},
            headers={"Idempotency-Key": "api-key"},
        )
        assert response.status_code == 202
        identity[0] = actors[2]
        catalog = api.get("/api/v1/repositories").json()
        assert catalog["total"] == 1
        detail = api.get("/api/v1/repositories/" + response.json()["repository_id"])
        assert detail.status_code == 200 and "storage_key" not in detail.json()
        assert api.get("/api/v1/repositories/" + str(uuid4())).status_code == 404
        assert api.get("/api/v1/repositories?page=0").status_code == 422
        app.dependency_overrides.clear()
        assert api.get("/api/v1/repositories").status_code == 401


def test_cancel_retry_recovery_and_old_token_publication(repo_env):
    settings, connections, service, actors, fixture = repo_env
    result = create(repo_env)
    task_id = result["task_id"]
    token, payload = service.tasks.claim(task_id)
    service.tasks.cancel(actors[1], task_id, str(uuid4()))
    assert not executor(repo_env).run(task_id, token, payload)
    assert service.detail(result["repository_id"])["status"] == "cancelled"
    assert not list(settings.repository_storage_root.glob("objects/*/*"))
    child = service.tasks.retry(actors[1], task_id, "retry-key", str(uuid4()))["task_id"]
    old_token, _ = service.tasks.claim(child)
    expire(connections.engine, "async_task", "lease_until", child)
    service.tasks.reconcile()
    latest = service.detail(result["repository_id"])["task"]["id"]
    assert latest != child
    assert not service.publish(child, old_token, {}, "old")
    next_token, payload = service.tasks.claim(latest)
    assert executor(repo_env).run(latest, next_token, payload)
    assert service.detail(result["repository_id"])["head_sha"] == fixture["head"]


def test_low_storage_prevents_records_and_runtime_write(repo_env):
    settings, connections, service, actors, fixture = repo_env
    low = Storage(settings, lambda _: SimpleNamespace(free=3 * GIB))
    service.storage = low
    with pytest.raises(RepositoryError) as error:
        create(repo_env)
    assert error.value.code == "REPOSITORY_STORAGE_LOW"
    service.storage = Storage(settings)
    result = create(repo_env)
    token, payload = service.tasks.claim(result["task_id"])
    runner = executor(repo_env)
    runner.storage = low
    assert not runner.run(result["task_id"], token, payload)
    assert service.detail(result["repository_id"])["task"]["error"]["code"] == (
        "REPOSITORY_STORAGE_LOW"
    )
    assert not list(settings.repository_storage_root.glob("objects/*/*"))


def test_ambiguous_publish_retains_files(repo_env, monkeypatch):
    settings, connections, service, actors, fixture = repo_env
    result = create(repo_env)
    token, payload = service.tasks.claim(result["task_id"])
    real_publish = service.publish

    def ambiguous(*args):
        assert real_publish(*args)
        raise RuntimeError("connection interrupted after commit")

    monkeypatch.setattr(service, "publish", ambiguous)
    with pytest.raises(RuntimeError, match="after commit"):
        executor(repo_env).run(result["task_id"], token, payload)
    with Session(connections.engine) as db:
        repo = db.get(Repository, result["repository_id"])
        assert repo.status == "cloned"
        assert (settings.repository_storage_root / repo.storage_key).exists()


def test_clone_size_limit_removes_only_unpublished_attempt(repo_env, monkeypatch):
    settings, _, service, _, _ = repo_env
    result = create(repo_env)
    token, payload = service.tasks.claim(result["task_id"])
    runner = executor(repo_env)
    # Isolate disk-size enforcement from the independent transport byte limit.
    monkeypatch.setattr(
        "packages.repositories.clone.directory_bytes", lambda _: settings.repository_max_bytes + 1
    )
    assert not runner.run(result["task_id"], token, payload)
    assert service.detail(result["repository_id"])["task"]["error"]["code"] == (
        "REPOSITORY_SIZE_LIMIT"
    )
    assert not list(settings.repository_storage_root.glob("objects/*/*"))
