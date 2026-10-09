"""Real TLS Git snapshots and MySQL: no production network/storage bypass."""

import hashlib
import os
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
from packages.persistence.models import (
    SCHEMA_HEAD,
    AsyncTask,
    GitCommit,
    ParseCheckpoint,
    Repository,
    SyncWindow,
)
from packages.repositories.clone import CloneExecutor
from packages.repositories.parser import ParseExecutor
from packages.repositories.parsing import ParsingService
from packages.repositories.safety import RepositoryError
from packages.repositories.service import RepositoryService
from packages.repositories.storage import GIB, Storage
from packages.repositories.sync import SyncExecutor, SyncService
from tests.auth_support import migrate, scratch_database
from tests.repository_support import git, https_git
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def sync_env(task_env, tmp_path, request):  # noqa: F811
    settings, connections, tasks, actors = task_env
    root = tmp_path / "repositories"
    root.mkdir()
    settings = settings.model_copy(update={"repository_storage_root": root})
    option = getattr(request, "param", False)
    with https_git(tmp_path / "http", empty=option is True) as http:
        if option == "recent":
            append((None, None, None, None, None, tmp_path / "http/served/team/demo.git"), 4)
        repositories = RepositoryService(settings, connections, probe=http["probe"])
        ack = repositories.create(
            actors[1], "fixture", "https://repo.example/team/demo.git", str(uuid4())
        )
        repo_id = ack["repository_id"]
        token, payload = tasks.claim(ack["task_id"])
        assert CloneExecutor(
            settings,
            repositories,
            policy=http["policy"],
            connector=http["connector"],
            cafile=http["certfile"],
        ).run(ack["task_id"], token, payload)
        parsing = ParsingService(settings, connections)
        task_id = parsing.create(
            actors[1], repo_id, "initial", 1 if option == "recent" else None, str(uuid4())
        )["task_id"]
        token, payload = tasks.claim(task_id)
        assert ParseExecutor(settings, parsing).run(task_id, token, payload)
        service = SyncService(settings, connections)
        served = tmp_path / "http/served/team/demo.git"
        yield settings, connections, service, actors, repo_id, served, http, task_id


def start(env, key=None):
    return env[2].create(env[3][1], env[4], key or str(uuid4()), str(uuid4()))["task_id"]


def execute(env, task_id):
    settings, _, service, _, _, _, http, _ = env
    token, payload = service.tasks.claim(task_id)
    clone = CloneExecutor(
        settings,
        service,
        policy=http["policy"],
        connector=http["connector"],
        cafile=http["certfile"],
    )
    return SyncExecutor(settings, service, clone).run(task_id, token, payload)


def append(env, count=1, parent=None, branch="main"):
    served = env[5]
    if parent is None:
        parent = git("-C", str(served), "rev-parse", branch).decode().strip()
    tree = git("-C", str(served), "rev-parse", parent + "^{tree}").decode().strip()
    commits = []
    for index in range(count):
        # Deliberately old/equal clocks; ancestry, not timestamp, defines the delta.
        variables = dict(
            os.environ,
            GIT_AUTHOR_NAME="Sync",
            GIT_AUTHOR_EMAIL="sync@example.invalid",
            GIT_COMMITTER_NAME="Sync",
            GIT_COMMITTER_EMAIL="sync@example.invalid",
            GIT_AUTHOR_DATE=f"{1600000000 + index // 2} +0800",
            GIT_COMMITTER_DATE=f"{1600000000 + index // 2} -0500",
        )
        parent = (
            git(
                "-C",
                str(served),
                "commit-tree",
                tree,
                "-p",
                parent,
                "-m",
                f"sync-{index}-{parent}",
                env=variables,
            )
            .decode()
            .strip()
        )
        commits.append(parent)
    git("-C", str(served), "update-ref", "refs/heads/" + branch, parent)
    return commits


def state(env):
    with Session(env[1].engine) as db:
        repo = db.get(Repository, env[4])
        return repo.head_sha, repo.storage_key, repo.sync_status


def fingerprint(path):
    digest = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            digest.update(p.relative_to(path).as_posix().encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()


def test_fast_forward_clocks_no_change_and_independent_windows(sync_env):
    settings, connections, service, _, repo_id, _, http, initial = sync_env
    base, old_key, _ = state(sync_env)
    old_bare = settings.repository_storage_root / old_key
    old_hash = fingerprint(old_bare)
    with Session(connections.engine) as db:
        old_ids = list(db.scalars(select(GitCommit.id)))
    delta = append(sync_env, 21)
    task_id = start(sync_env, "first-sync")
    assert execute(sync_env, task_id)
    assert state(sync_env)[0] == delta[-1] and state(sync_env)[1] != old_key
    assert fingerprint(old_bare) == old_hash
    with Session(connections.engine) as db:
        point = db.get(SyncWindow, task_id)
        assert point.base_head_sha == base and point.processed == point.total == 21
        assert point.relation == "fast_forward"
        assert db.get(ParseCheckpoint, initial).head_sha == base
        assert all(db.get(GitCommit, identifier) for identifier in old_ids)
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 22
    assert start(sync_env, "first-sync") == task_id
    unchanged = start(sync_env)
    assert execute(sync_env, unchanged)
    with Session(connections.engine) as db:
        point = db.get(SyncWindow, unchanged)
        assert point.total == point.processed == 0 and point.relation == "unchanged"
    append(sync_env, 2)
    next_task = start(sync_env)
    assert execute(sync_env, next_task)
    assert service.windows(repo_id, page_size=1)["total"] == 3
    with Session(connections.engine) as db:
        assert db.get(SyncWindow, next_task).total == 2
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 24
    assert fingerprint(old_bare) == old_hash
    assert len(http["state"]["requests"]) > 4
    migrate(connections.engine, "check")


@pytest.mark.parametrize("change", ["missing_old", "old_still_present", "branch_changed", "empty"])
def test_rewritten_or_changed_branch_requires_review_without_deleting(sync_env, change):
    settings, connections, service, _, repo_id, served, _, initial = sync_env
    base, old_key, _ = state(sync_env)
    old_hash = fingerprint(settings.repository_storage_root / old_key)
    if change in {"missing_old", "old_still_present"}:
        tree = git("-C", str(served), "rev-parse", base + "^{tree}").decode().strip()
        variables = dict(
            os.environ,
            GIT_AUTHOR_NAME="Rewrite",
            GIT_AUTHOR_EMAIL="rewrite@example.invalid",
            GIT_COMMITTER_NAME="Rewrite",
            GIT_COMMITTER_EMAIL="rewrite@example.invalid",
        )
        rewritten = (
            git("-C", str(served), "commit-tree", tree, "-m", "new root", env=variables)
            .decode()
            .strip()
        )
        if change == "old_still_present":
            git("-C", str(served), "update-ref", "refs/heads/archive", base)
        git("-C", str(served), "update-ref", "refs/heads/main", rewritten)
    elif change == "branch_changed":
        git("-C", str(served), "update-ref", "refs/heads/renamed", base)
        git("-C", str(served), "symbolic-ref", "HEAD", "refs/heads/renamed")
    else:
        git("-C", str(served), "update-ref", "-d", "refs/heads/main")
    task_id = start(sync_env)
    assert not execute(sync_env, task_id)
    assert state(sync_env) == (base, old_key, "requires_review")
    with Session(connections.engine) as db:
        point, task = db.get(SyncWindow, task_id), db.get(AsyncTask, task_id)
        assert point.relation == "requires_review" and point.processed == 0
        assert task.status == "succeeded" and task.result_json["disposition"] == "requires_review"
        assert (settings.repository_storage_root / point.storage_key).is_dir()
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 1
        assert db.get(ParseCheckpoint, initial).head_sha == base
    with pytest.raises(RepositoryError) as failure:
        start(sync_env)
    assert failure.value.code == "REPOSITORY_SYNC_STATE_CONFLICT"
    assert fingerprint(settings.repository_storage_root / old_key) == old_hash


@pytest.mark.parametrize("fault", ["cancel", "lease", "space"])
def test_sync_batch_resume_same_snapshot_and_fence(sync_env, monkeypatch, fault):
    settings, connections, service, actors, _, _, http, _ = sync_env
    base, old_key, _ = state(sync_env)
    append(sync_env, 21)
    task_id = start(sync_env)
    original = service.batch

    def interrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[2] == 0:
            if fault == "cancel":
                service.tasks.cancel(actors[1], task_id, str(uuid4()))
            elif fault == "lease":
                expire(connections.engine, "async_task", "lease_until", task_id)
            else:
                service.storage.usage = lambda _: SimpleNamespace(free=2 * GIB)
        return result

    monkeypatch.setattr(service, "batch", interrupt)
    assert not execute(sync_env, task_id)
    with Session(connections.engine) as db:
        point = db.get(SyncWindow, task_id)
        candidate = point.storage_key
        assert point.processed == 10
        old_token = db.get(AsyncTask, task_id).execution_token
    assert state(sync_env)[:2] == (base, old_key)
    assert not service.heartbeat(task_id, str(uuid4()))
    if fault == "lease":
        service.tasks.reconcile()
        with Session(connections.engine) as db:
            child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id)).id
    else:
        child = service.tasks.retry(actors[1], task_id, "retry", str(uuid4()))["task_id"]
    service.storage.usage = Storage(settings).usage
    monkeypatch.setattr(service, "batch", original)
    requests_before = len(http["state"]["requests"])
    assert execute(sync_env, child)
    assert len(http["state"]["requests"]) == requests_before
    assert not service.heartbeat(task_id, old_token)
    with Session(connections.engine) as db:
        point = db.get(SyncWindow, task_id)
        assert point.storage_key == candidate and point.processed == point.total == 21
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 22
    assert state(sync_env)[1] == candidate


def test_final_transaction_rollback_and_unknown_publication_ack(sync_env, monkeypatch):
    settings, connections, service, actors, _, _, _, _ = sync_env
    base = state(sync_env)[:2]
    append(sync_env, 2)
    task_id = start(sync_env)
    original = Session.flush
    fired = [False]

    def fail_flush(db, *args, **kwargs):
        if not fired[0] and any(isinstance(p, SyncWindow) and p.processed for p in db.dirty):
            fired[0] = True
            raise RuntimeError("final transaction unavailable")
        return original(db, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="final transaction"):
        execute(sync_env, task_id)
    assert state(sync_env)[:2] == base
    with Session(connections.engine) as db:
        assert db.get(SyncWindow, task_id).processed == 0
        candidate = db.get(SyncWindow, task_id).storage_key
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 1
    expire(connections.engine, "async_task", "lease_until", task_id)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id)).id
    monkeypatch.setattr(Session, "flush", original)
    batch = service.batch

    def lose_ack(*args, **kwargs):
        batch(*args, **kwargs)
        raise RuntimeError("committed acknowledgement lost")

    monkeypatch.setattr(service, "batch", lose_ack)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        execute(sync_env, child)
    assert state(sync_env)[1] == candidate
    assert (settings.repository_storage_root / candidate).is_dir()
    with Session(connections.engine) as db:
        assert db.get(AsyncTask, child).status == "succeeded"
        assert db.get(SyncWindow, task_id).processed == 2


def test_concurrent_api_rbac_capacity_and_nonempty_downgrade(sync_env):
    settings, connections, service, actors, repo_id, _, _, _ = sync_env
    low = Storage(settings, usage=lambda _: SimpleNamespace(free=2 * GIB))
    service.storage = low
    with pytest.raises(RepositoryError) as failure:
        start(sync_env)
    assert failure.value.code == "REPOSITORY_STORAGE_LOW"
    with Session(connections.engine) as db:
        assert not db.scalar(select(SyncWindow))
    service.storage = Storage(settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: start(sync_env, "same-key"), range(2)))
    assert ids[0] == ids[1]
    app = create_app(settings)
    actor = [actors[2]]
    app.dependency_overrides[current_user] = lambda: actor[0]
    path = f"/api/v1/repositories/{repo_id}"
    with TestClient(app, base_url="http://127.0.0.1") as api:
        assert (
            api.post(path + "/sync", json={}, headers={"Idempotency-Key": "api"}).status_code == 403
        )
        assert api.get(path + "/sync-windows?page_size=1").json()["total"] == 1
        actor[0] = actors[1]
        assert (
            api.post(
                path + "/sync",
                json={"url": "https://other.example"},
                headers={"Idempotency-Key": "api"},
            ).status_code
            == 422
        )
        replay = api.post(path + "/sync", json={}, headers={"Idempotency-Key": "same-key"})
        assert replay.status_code == 202 and replay.json()["task_id"] == ids[0]
        assert (
            api.post(path + "/sync", json={}, headers={"Idempotency-Key": "different"}).status_code
            == 409
        )
    for target in ("0004_commit_parsing", "0003_repositories", "0002_tasks", "0001_auth", "base"):
        with pytest.raises(RuntimeError, match="Downgrade refused"):
            migrate(connections.engine, "downgrade", target)
        with connections.engine.connect() as db:
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar() == SCHEMA_HEAD
            )
    with pytest.raises(DBAPIError), connections.engine.begin() as db:
        db.execute(text("UPDATE sync_window SET processed=-1"))
    with pytest.raises(DBAPIError), connections.engine.begin() as db:
        db.execute(text("UPDATE repository SET sync_status='anything'"))


@pytest.mark.parametrize("sync_env", [True], indirect=True)
def test_empty_to_first_commits(sync_env):
    settings, connections, service, _, _, served, _, initial = sync_env
    source = served.parents[2] / "source"
    (source / "new.txt").write_text("first\n", encoding="utf-8")
    git("-C", str(source), "add", ".")
    git("-C", str(source), "commit", "-m", "first")
    git("-C", str(source), "push", str(served), "main")
    task_id = start(sync_env)
    assert execute(sync_env, task_id)
    with Session(connections.engine) as db:
        point = db.get(SyncWindow, task_id)
        assert point.relation == "initial" and point.processed == point.total == 1
        assert db.get(ParseCheckpoint, initial).head_sha is None


def test_empty_schema_roundtrip_and_old_data_upgrade():
    with scratch_database() as (_, engine):
        migrate(engine, target="0004_commit_parsing")
        # Existing real fixture data is covered above; empty whole-path downgrade here.
        migrate(engine)
        migrate(engine, "check")
        migrate(engine, "downgrade", "0004_commit_parsing")
        migrate(engine)
        migrate(engine, "downgrade", "base")
        migrate(engine)
        migrate(engine, "check")


def test_upgrade_preserves_existing_parsed_history(sync_env):
    settings, connections, _, _, repo_id, _, _, initial = sync_env
    before = state(sync_env)[:2]
    with Session(connections.engine) as db:
        old_ids = list(db.scalars(select(GitCommit.id)))
    migrate(connections.engine, "downgrade", "0004_commit_parsing")
    migrate(connections.engine)
    with Session(connections.engine) as db:
        assert db.get(Repository, repo_id).sync_status == "pending"
        assert db.get(ParseCheckpoint, initial).head_sha == before[0]
        assert all(db.get(GitCommit, identifier) for identifier in old_ids)
    assert state(sync_env)[:2] == before
    assert execute(sync_env, start(sync_env))


def test_snapshot_publication_ack_lost_keeps_fixed_remote_head(sync_env, monkeypatch):
    settings, connections, service, _, _, _, http, _ = sync_env
    base = state(sync_env)[:2]
    first_head = append(sync_env)[-1]
    task_id = start(sync_env)
    original = service.publish

    def uncertain(*args, **kwargs):
        assert original(*args, **kwargs)
        raise RuntimeError("snapshot commit acknowledgement lost")

    monkeypatch.setattr(service, "publish", uncertain)
    with pytest.raises(RuntimeError, match="snapshot commit"):
        execute(sync_env, task_id)
    with Session(connections.engine) as db:
        candidate = db.get(SyncWindow, task_id).storage_key
        assert db.get(SyncWindow, task_id).head_sha == first_head
    assert (settings.repository_storage_root / candidate).is_dir()
    assert state(sync_env)[:2] == base
    append(sync_env, 2)
    expire(connections.engine, "async_task", "lease_until", task_id)
    service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id)).id
    requests = len(http["state"]["requests"])
    assert execute(sync_env, child)
    assert len(http["state"]["requests"]) == requests
    assert state(sync_env)[:2] == (first_head, candidate)
    with Session(connections.engine) as db:
        assert db.get(SyncWindow, task_id).processed == 1


@pytest.mark.parametrize("sync_env", ["recent"], indirect=True)
def test_recent_window_never_becomes_full_history_after_sync(sync_env):
    settings, connections, service, _, repo_id, _, _, initial = sync_env
    append(sync_env, 2)
    assert execute(sync_env, start(sync_env))
    detail = RepositoryService(settings, connections).detail(repo_id)
    assert detail["history_coverage"] == "recent_window"
    assert detail["parse_window"]["commit_limit"] == 1
    with Session(connections.engine) as db:
        assert db.get(ParseCheckpoint, initial).processed == 1
        assert db.scalar(select(func.count()).select_from(GitCommit)) == 3
