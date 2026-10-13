"""Actual Worker kill/resume preserves reviewed evidence and accepted snapshot."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.mining.fix import FixService
from packages.persistence.models import AsyncTask, FixAssessment, FixRun, Repository
from packages.tasks.service import TaskService
from tests.parsing_support import history
from tests.repository_support import git
from tests.test_runtime import docker
from tests.test_sync_runtime import seed
from tests.test_tasks import expire
from tests.test_tasks_runtime import live, wait_task  # noqa: F401

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1",
        reason="explicit Docker fault opt-in required",
    ),
]


def test_actual_fix_worker_kill_preserves_review_and_resumes(live, tmp_path):  # noqa: F811
    api, settings, connections = live
    source, shas = history(tmp_path / "source")
    head = shas["iteration-5"]
    tree = git("-C", str(source), "rev-parse", "HEAD^{tree}").decode().strip()
    for index in range(66):
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Fix Runtime",
            GIT_AUTHOR_EMAIL="fix-runtime@example.invalid",
            GIT_COMMITTER_NAME="Fix Runtime",
            GIT_COMMITTER_EMAIL="fix-runtime@example.invalid",
            GIT_AUTHOR_DATE=f"{1700000000 + index} +0000",
            GIT_COMMITTER_DATE=f"{1700000000 + index} +0000",
        )
        head = (
            git("-C", str(source), "commit-tree", tree, "-p", head, "-m", "Fix crash", env=env)
            .decode()
            .strip()
        )
    git("-C", str(source), "update-ref", "refs/heads/main", head)
    bare = tmp_path / "fixture.git"
    git("clone", "--bare", str(source), str(bare))
    repo_id = str(uuid4())
    key = seed(bare, repo_id)
    actor = api.get("/api/v1/users/me").json()["id"]
    tasks = TaskService(settings, connections)
    with Session(connections.engine) as db, db.begin():
        clone = tasks.new_task(
            db,
            actor,
            "clone-" + repo_id,
            {"repository_id": repo_id},
            "runtime-fixture",
            str(uuid4()),
            kind="repository.clone",
        )
        clone.status = clone.stage = "succeeded"
        db.add(
            Repository(
                id=repo_id,
                owner_id=actor,
                canonical_url=f"https://fixture.example/fix/{repo_id}",
                status="cloned",
                latest_task_id=clone.id,
                head_sha=head,
                default_branch="main",
                storage_key=key,
                size_bytes=1,
            )
        )
    path = "/api/v1/repositories/" + repo_id
    result = api.post(path + "/parse", json={}, headers={"Idempotency-Key": str(uuid4())})
    assert result.status_code == 202
    assert wait_task(api, result.json()["task_id"], {"succeeded"}, timeout=150)["processed"] == 80
    result = api.post(path + "/fix-detection", json={}, headers={"Idempotency-Key": str(uuid4())})
    assert result.status_code == 202
    root = result.json()["task_id"]
    try:
        deadline = time.monotonic() + 75
        observed = False
        while time.monotonic() < deadline:
            with Session(connections.engine) as db:
                point, task = db.get(FixRun, root), db.get(AsyncTask, root)
                if 0 < point.processed < 80 and task.status == "running":
                    token = task.execution_token
                    observed = True
                    break
            time.sleep(0.02)
        assert observed, "Actual Fix worker must expose a committed partial batch"
        docker("kill", "-s", "SIGKILL", "worker")
        with Session(connections.engine) as db:
            point = db.get(FixRun, root)
            persisted = point.processed
            assert 0 < persisted < 80
            row = db.scalar(
                select(FixAssessment)
                .where(FixAssessment.root_task_id == root)
                .order_by(FixAssessment.sha)
            )
            assessment_id = row.id
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(FixAssessment)
                    .where(FixAssessment.root_task_id == root)
                )
                == persisted
            )
        reviewed = api.patch(
            path + f"/fix-evidence/{assessment_id}/review",
            json={
                "status": "confirmed",
                "expected_revision": 0,
                "note": "runtime preserved review",
            },
        )
        assert reviewed.status_code == 200 and reviewed.json()["review_revision"] == 1
        expire(connections.engine, "async_task", "lease_until", root)
        assert wait_task(api, root, {"failed"}, timeout=45)["error"]["code"] == "TASK_LEASE_EXPIRED"
        with Session(connections.engine) as db:
            child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root))
            assert child.root_task_id == root and child.type == "repository.fix"
            successor = child.id
        assert not FixService(settings, connections).heartbeat(root, token)
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
    assert wait_task(api, successor, {"succeeded"}, timeout=120)["processed"] == 80
    with Session(connections.engine) as db:
        point = db.get(FixRun, root)
        assert (
            point.total == point.processed == 80
            and point.head_sha == head
            and point.storage_key == key
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(FixAssessment)
                .where(FixAssessment.root_task_id == root)
            )
            == 80
        )
        row = db.get(FixAssessment, assessment_id)
        assert (
            row.review_revision == 1
            and row.review_status == "confirmed"
            and row.review_note == "runtime preserved review"
        )
        repo = db.get(Repository, repo_id)
        assert repo.head_sha == head and repo.storage_key == key and repo.fix_status == "detected"
