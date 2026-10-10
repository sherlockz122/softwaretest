"""Real Worker SIGKILL after a committed batch; immutable fixture seeded into D bind."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.persistence.models import AsyncTask, GitCommit, ParseCheckpoint, Repository
from packages.repositories.parsing import ParsingService
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


def test_real_parser_kill_preserves_batch_and_successor_resumes(live, tmp_path):  # noqa: F811
    api, settings, connections = live
    source, shas = history(tmp_path / "source")
    head = shas["iteration-5"]
    tree = git("-C", str(source), "rev-parse", "HEAD^{tree}").decode().strip()
    for index in range(60):
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Resume",
            GIT_AUTHOR_EMAIL="resume@example.invalid",
            GIT_COMMITTER_NAME="Resume",
            GIT_COMMITTER_EMAIL="resume@example.invalid",
            GIT_AUTHOR_DATE=f"{1700000000 + index} +0000",
            GIT_COMMITTER_DATE=f"{1700000000 + index} +0000",
        )
        head = (
            git("-C", str(source), "commit-tree", tree, "-p", head, "-m", "resume", env=env)
            .decode()
            .strip()
        )
    git("-C", str(source), "update-ref", "refs/heads/main", head)
    bare = tmp_path / "fixture.git"
    git("clone", "--bare", str(source), str(bare))
    repo_id, clone_token = str(uuid4()), str(uuid4())
    key = f"objects/{repo_id}/{clone_token}/repo.git"
    # Container-root operation touches only this test's two validated UUID levels;
    # it does not change host accounts, existing repository data, or SSH permissions.
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
        clone_token,
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
        "p=safe_path(load_settings().repository_storage_root/'objects'/str(UUID(sys.argv[1]))); "
        "os.chown(p,10001,10001); "
        "[(os.chown(safe_path(os.path.join(r,n)),10001,10001)) "
        "for r,ds,fs in os.walk(p,followlinks=False) for n in ds+fs]",
        repo_id,
    )
    actor_id = api.get("/api/v1/users/me").json()["id"]
    tasks = TaskService(settings, connections)
    with Session(connections.engine) as db, db.begin():
        task = tasks.new_task(
            db,
            actor_id,
            "clone-" + repo_id,
            {"repository_id": repo_id},
            "runtime-fixture",
            str(uuid4()),
            kind="repository.clone",
        )
        task.status = task.stage = "succeeded"
        db.add(
            Repository(
                id=repo_id,
                owner_id=actor_id,
                canonical_url=f"https://fixture.example/runtime/{repo_id}",
                status="cloned",
                latest_task_id=task.id,
                head_sha=head,
                default_branch="main",
                storage_key=key,
                size_bytes=1,
            )
        )
    response = api.post(
        f"/api/v1/repositories/{repo_id}/parse", json={}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    try:
        deadline = time.monotonic() + 60
        observed = False
        while time.monotonic() < deadline:
            with Session(connections.engine) as db:
                point = db.get(ParseCheckpoint, task_id)
                task = db.get(AsyncTask, task_id)
                if 0 < point.processed < 74 and task.status == "running":
                    old_token = task.execution_token
                    observed = True
                    break
            time.sleep(0.05)
        assert observed, "Real Worker did not expose a committed partial batch"
        docker("kill", "-s", "SIGKILL", "worker")
        with Session(connections.engine) as db:
            persisted = db.get(ParseCheckpoint, task_id).processed
            assert 0 < persisted < 74
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(GitCommit)
                    .where(GitCommit.repository_id == repo_id)
                )
                == persisted
            )
        expire(connections.engine, "async_task", "lease_until", task_id)
        result = wait_task(api, task_id, {"failed"}, timeout=45)
        assert result["error"]["code"] == "TASK_LEASE_EXPIRED"
        with Session(connections.engine) as db:
            child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id))
            assert child.root_task_id == task_id and child.type == "repository.parse"
            successor = child.id
        assert not ParsingService(settings, connections).heartbeat(task_id, old_token)
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
    assert wait_task(api, successor, {"succeeded"}, timeout=90)["processed"] == 74
    with Session(connections.engine) as db:
        point = db.get(ParseCheckpoint, task_id)
        assert point.processed == point.total == 74 and point.head_sha == head
        assert (
            db.scalar(
                select(func.count())
                .select_from(GitCommit)
                .where(GitCommit.repository_id == repo_id)
            )
            == 74
        )
        assert db.get(Repository, repo_id).parse_status == "parsed"
