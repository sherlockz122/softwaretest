"""Actual Linux Worker SIGKILL: resume a published immutable sync candidate on D."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.persistence.models import (
    AsyncTask,
    GitCommit,
    ParseCheckpoint,
    Repository,
    SyncWindow,
)
from packages.repositories.parse_policy import PARSER_VERSION
from packages.repositories.sync import SYNC_VERSION, SyncService
from packages.tasks.service import TaskService
from tests.parsing_support import history
from tests.repository_support import git
from tests.test_runtime import docker, raw_docker
from tests.test_tasks import expire
from tests.test_tasks_runtime import live, wait_task  # noqa: F401

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1",
        reason="explicit Docker fault opt-in required",
    ),
]


def seed(bare, repo_id):
    token = str(uuid4())
    key = f"objects/{repo_id}/{token}/repo.git"
    docker(
        "exec",
        "-T",
        "--user",
        "0",
        "worker",
        "python",
        "-c",
        "import sys; from packages.platform.config import load_settings; "
        "from packages.repositories.storage import Storage; "
        "Storage(load_settings()).attempt(sys.argv[1],sys.argv[2])",
        repo_id,
        token,
    )
    container = docker("ps", "-q", "worker")
    raw_docker("cp", str(bare), container + ":/app/runtime/repositories/" + key)
    docker(
        "exec",
        "-T",
        "--user",
        "0",
        "worker",
        "python",
        "-c",
        "import os,sys; from uuid import UUID; "
        "from packages.platform.config import load_settings; "
        "from packages.repositories.storage import safe_path; "
        "r=safe_path(load_settings().repository_storage_root/'objects'/str(UUID(sys.argv[1]))); "
        "p=safe_path(r/str(UUID(sys.argv[2]))); os.chown(r,10001,10001); os.chown(p,10001,10001); "
        "[(os.chown(safe_path(os.path.join(d,n)),10001,10001)) "
        "for d,ds,fs in os.walk(p,followlinks=False) for n in ds+fs]",
        repo_id,
        token,
    )
    return key


def test_real_sync_worker_kill_retains_base_candidate_and_atomic_resume(live, tmp_path):  # noqa: F811
    api, settings, connections = live
    source, shas = history(tmp_path / "source")
    base = shas["iteration-5"]
    old_bare = tmp_path / "base.git"
    git("clone", "--bare", str(source), str(old_bare))
    repo_id = str(uuid4())
    old_key = seed(old_bare, repo_id)
    actor_id = api.get("/api/v1/users/me").json()["id"]
    tasks = TaskService(settings, connections)
    with Session(connections.engine) as db, db.begin():
        task = tasks.new_task(
            db,
            actor_id,
            "base-" + repo_id,
            {"repository_id": repo_id},
            "sync-runtime-fixture",
            str(uuid4()),
            kind="repository.clone",
        )
        task.status = task.stage = "succeeded"
        db.add(
            Repository(
                id=repo_id,
                owner_id=actor_id,
                canonical_url=f"https://fixture.example/sync/{repo_id}",
                status="cloned",
                latest_task_id=task.id,
                head_sha=base,
                default_branch="main",
                storage_key=old_key,
                size_bytes=1,
            )
        )
    response = api.post(
        f"/api/v1/repositories/{repo_id}/parse", json={}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 202
    initial_id = response.json()["task_id"]
    assert wait_task(api, initial_id, {"succeeded"})["processed"] == 14
    tree = git("-C", str(source), "rev-parse", "HEAD^{tree}").decode().strip()
    head = base
    for index in range(70):
        variables = dict(
            os.environ,
            GIT_AUTHOR_NAME="Sync",
            GIT_AUTHOR_EMAIL="sync@example.invalid",
            GIT_COMMITTER_NAME="Sync",
            GIT_COMMITTER_EMAIL="sync@example.invalid",
            GIT_AUTHOR_DATE=f"{1600000040 + index} +0000",
            GIT_COMMITTER_DATE=f"{1600000040 + index} +0000",
        )
        head = (
            git("-C", str(source), "commit-tree", tree, "-p", head, "-m", "sync", env=variables)
            .decode()
            .strip()
        )
    git("-C", str(source), "update-ref", "refs/heads/main", head)
    new_bare = tmp_path / "candidate.git"
    git("clone", "--bare", str(source), str(new_bare))
    candidate_key = seed(new_bare, repo_id)
    with Session(connections.engine) as db, db.begin():
        repo = db.get(Repository, repo_id, with_for_update=True)
        task = tasks.new_task(
            db,
            actor_id,
            "sync-" + repo_id,
            {
                "repository_id": repo_id,
                "url": repo.canonical_url,
                "parser_version": PARSER_VERSION,
                "sync_version": SYNC_VERSION,
            },
            "sync-runtime-fixture",
            str(uuid4()),
            kind="repository.sync",
        )
        task_id = task.id
        repo.latest_task_id = repo.sync_root_task_id = task_id
        repo.sync_status = "queued"
        db.add(
            SyncWindow(
                root_task_id=task_id,
                repository_id=repo_id,
                base_head_sha=base,
                base_storage_key=old_key,
                base_branch="main",
                head_sha=head,
                storage_key=candidate_key,
                default_branch="main",
                size_bytes=1,
                parser_version=PARSER_VERSION,
            )
        )
    try:
        deadline = time.monotonic() + 75
        observed = False
        while time.monotonic() < deadline:
            with Session(connections.engine) as db:
                point, task = db.get(SyncWindow, task_id), db.get(AsyncTask, task_id)
                if 0 < point.processed < 70 and task.status == "running":
                    old_token = task.execution_token
                    observed = True
                    break
            time.sleep(0.05)
        assert observed, "Actual sync Worker did not expose a committed partial batch"
        docker("kill", "-s", "SIGKILL", "worker")
        with Session(connections.engine) as db:
            point = db.get(SyncWindow, task_id)
            assert 0 < point.processed < 70
            repo = db.get(Repository, repo_id)
            assert repo.head_sha == base and repo.storage_key == old_key
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(GitCommit)
                    .where(GitCommit.repository_id == repo_id)
                )
                == 14 + point.processed
            )
        expire(connections.engine, "async_task", "lease_until", task_id)
        assert (
            wait_task(api, task_id, {"failed"}, timeout=45)["error"]["code"] == "TASK_LEASE_EXPIRED"
        )
        with Session(connections.engine) as db:
            child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id))
            assert child.root_task_id == task_id and child.type == "repository.sync"
            successor = child.id
        assert not SyncService(settings, connections).heartbeat(task_id, old_token)
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
    assert wait_task(api, successor, {"succeeded"}, timeout=120)["processed"] == 70
    with Session(connections.engine) as db:
        point, repo = db.get(SyncWindow, task_id), db.get(Repository, repo_id)
        assert point.total == point.processed == 70 and point.storage_key == candidate_key
        assert (
            repo.head_sha == head
            and repo.storage_key == candidate_key
            and repo.sync_status == "synced"
        )
        assert db.get(ParseCheckpoint, initial_id).head_sha == base
        assert (
            db.scalar(
                select(func.count())
                .select_from(GitCommit)
                .where(GitCommit.repository_id == repo_id)
            )
            == 84
        )
    for key in (old_key, candidate_key):
        assert (settings.repository_storage_root / key).is_dir()
