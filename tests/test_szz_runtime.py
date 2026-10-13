"""Actual SZZ Worker kill/resume retains frozen inputs despite later Fix review."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.mining.szz import SZZService
from packages.persistence.models import (
    AsyncTask,
    FixAssessment,
    Repository,
    SZZItem,
    SZZLink,
    SZZRun,
)
from packages.tasks.service import TaskService
from tests.repository_support import git
from tests.szz_support import szz_history
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


def test_actual_szz_worker_kill_frozen_review_and_resume(live, tmp_path):  # noqa: F811
    api, settings, connections = live
    source, _, _, shas = szz_history(tmp_path / "source", count=78)
    head = shas[-1]
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
                canonical_url=f"https://fixture.example/szz/{repo_id}",
                status="cloned",
                latest_task_id=clone.id,
                head_sha=head,
                default_branch="main",
                storage_key=key,
                size_bytes=1,
            )
        )
    path = "/api/v1/repositories/" + repo_id
    for suffix in ("/parse", "/fix-detection"):
        response = api.post(path + suffix, json={}, headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 202
        assert (
            wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=180)["processed"]
            == 80
        )
    fixed = response.json()["task_id"]
    response = api.post(
        path + "/szz-runs", json={"fix_run_id": fixed}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 202
    root = response.json()["task_id"]
    try:
        deadline = time.monotonic() + 90
        observed = False
        while time.monotonic() < deadline:
            with Session(connections.engine) as db:
                point, task = db.get(SZZRun, root), db.get(AsyncTask, root)
                if 0 < point.processed < 80 and task.status == "running":
                    token = task.execution_token
                    frozen_digest = point.source_digest
                    observed = True
                    break
            time.sleep(0.02)
        assert observed, "Actual SZZ Worker must expose a committed partial batch"
        docker("kill", "-s", "SIGKILL", "worker")
        with Session(connections.engine) as db:
            point = db.get(SZZRun, root)
            assert 0 < point.processed < 80
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(SZZItem)
                    .where(SZZItem.root_task_id == root, SZZItem.result_json.is_not(None))
                )
                == point.processed
            )
            row = db.scalar(
                select(FixAssessment).where(
                    FixAssessment.root_task_id == fixed, FixAssessment.sha == shas[1]
                )
            )
            assessment_id = row.id
        changed = api.patch(
            path + f"/fix-evidence/{assessment_id}/review",
            json={
                "status": "rejected",
                "expected_revision": 0,
                "note": "after SZZ frozen runtime input",
            },
        )
        assert changed.status_code == 200 and changed.json()["review_revision"] == 1
        expire(connections.engine, "async_task", "lease_until", root)
        assert wait_task(api, root, {"failed"}, timeout=45)["error"]["code"] == "TASK_LEASE_EXPIRED"
        with Session(connections.engine) as db:
            child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root))
            assert child.root_task_id == root and child.type == "repository.szz"
            successor = child.id
        assert not SZZService(settings, connections).heartbeat(root, token)
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
    assert wait_task(api, successor, {"succeeded"}, timeout=180)["processed"] == 80
    with Session(connections.engine) as db:
        point = db.get(SZZRun, root)
        assert (
            point.total == point.processed == 80
            and point.head_sha == head
            and point.storage_key == key
            and point.source_digest == frozen_digest
        )
        item = db.scalar(
            select(SZZItem).where(SZZItem.root_task_id == root, SZZItem.sha == shas[1])
        )
        assert item.input_json["candidate"] and item.input_json["review_revision"] == 0
        assert db.get(FixAssessment, assessment_id).review_revision == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(SZZLink)
                .join(SZZItem, SZZLink.item_id == SZZItem.id)
                .where(SZZItem.root_task_id == root)
            )
            == 79
        )
        repo = db.get(Repository, repo_id)
        assert repo.head_sha == head and repo.storage_key == key and repo.szz_status == "traced"
