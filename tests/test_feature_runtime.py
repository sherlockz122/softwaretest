"""Actual Worker SIGKILL after feature publication; resume the frozen input run."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.mining.feature_service import FeatureService
from packages.persistence.models import (
    AsyncTask,
    FeatureItem,
    FeatureRun,
    FixAssessment,
    Repository,
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


def test_actual_feature_worker_kill_frozen_review_and_resume(live, tmp_path):  # noqa: F811
    api, settings, connections = live
    source, _, _, shas = szz_history(tmp_path / "source", count=398)
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
                canonical_url=f"https://fixture.example/features/{repo_id}",
                status="cloned",
                latest_task_id=clone.id,
                head_sha=shas[-1],
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
            wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=240)["processed"]
            == 400
        )
    fixed = response.json()["task_id"]
    response = api.post(
        path + "/feature-runs",
        json={"fix_run_id": fixed},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202
    root = response.json()["task_id"]
    started = time.monotonic()
    try:
        deadline = time.monotonic() + 180
        observed = False
        while time.monotonic() < deadline:
            with Session(connections.engine) as db:
                point, task = db.get(FeatureRun, root), db.get(AsyncTask, root)
                if 0 < point.processed < 400 and task.status == "running":
                    token = task.execution_token
                    frozen_digest = point.source_digest
                    observed = True
                    break
            time.sleep(0.02)
        assert observed, "Actual feature Worker must expose a durable partial batch"
        docker("kill", "-s", "SIGKILL", "worker")
        with Session(connections.engine) as db:
            point = db.get(FeatureRun, root)
            assert 0 < point.processed < 400 and point.quality_json is None
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(FeatureItem)
                    .where(FeatureItem.root_task_id == root, FeatureItem.result_json.is_not(None))
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
                "note": "after feature freeze",
            },
        )
        assert changed.status_code == 200
        expire(connections.engine, "async_task", "lease_until", root)
        assert wait_task(api, root, {"failed"}, timeout=45)["error"]["code"] == "TASK_LEASE_EXPIRED"
        with Session(connections.engine) as db:
            child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root))
            assert child.type == "repository.feature" and child.root_task_id == root
            successor = child.id
        assert not FeatureService(settings, connections).heartbeat(root, token)
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
    assert wait_task(api, successor, {"succeeded"}, timeout=180)["processed"] == 400
    with Session(connections.engine) as db:
        point = db.get(FeatureRun, root)
        assert point.total == point.processed == point.quality_json["count"] == 400
        assert point.source_digest == frozen_digest and point.head_sha == shas[-1]
        assert point.fix_json[shas[1]]["review_revision"] == 0
        assert db.get(FixAssessment, assessment_id).review_revision == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(FeatureItem)
                .where(FeatureItem.root_task_id == root)
            )
            == 400
        )
    print(
        f"FEATURE_RUNTIME commits=400 elapsed_seconds={time.monotonic() - started:.3f} "
        "recovered=true"
    )
