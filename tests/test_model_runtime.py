"""Opt-in real Worker interruption; engineering data never proves model accuracy."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.persistence.models import AsyncTask, ModelRun
from tests.dataset_support import fixture_asset
from tests.test_tasks_runtime import docker, runtime_client, wait_ready, wait_task

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1",
        reason="explicit local fault-test opt-in required",
    ),
]


def test_real_model_worker_kill_retry_registration_and_old_attempt():
    root = assets.ROOT / "runtime/temp" / ("model-runtime-" + uuid4().hex)
    root.mkdir(parents=True)
    directory, imported = fixture_asset(
        root, rows=4000, store=assets.ROOT / "runtime/public-assets"
    )
    # Inputs/failed attempts remain locally attributable, not global cleanup candidates.
    with runtime_client() as (api, settings, connections):
        response = api.post(
            "/api/v1/datasets",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "name": "engineering Worker recovery",
                "asset_id": directory.name,
                "source_hash": imported["manifest"]["content_hash"],
            },
        )
        assert response.status_code == 202
        dataset_id = response.json()["dataset_id"]
        wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=90)
        body = {
            "dataset_id": dataset_id,
            "plugin": "rf",
            "params": {"n_estimators": 256, "max_depth": 32, "min_samples_leaf": 1},
            "seed": 42,
        }
        response = api.post(
            "/api/v1/model-runs", headers={"Idempotency-Key": uuid4().hex}, json=body
        )
        assert response.status_code == 202
        task_id, run_id = response.json()["task_id"], response.json()["model_run_id"]
        wait_task(api, task_id, {"running"})
        with Session(connections.engine) as db:
            old_token = db.get(AsyncTask, task_id).execution_token
        deadline = time.monotonic() + 30
        # Linux host runner must not bypass the private 10001-owned model bind root.
        status = f"/app/runtime/models/{run_id}/{old_token}/stage.txt"
        probe = f"from pathlib import Path; print(int(Path({status!r}).is_file()))"
        started = False
        while time.monotonic() < deadline:
            if docker("exec", "-T", "worker", "python", "-c", probe) == "1":
                started = True
                break
            time.sleep(0.1)
        assert started, "training subprocess must actually start before SIGKILL"
        try:
            docker("kill", "--signal", "SIGKILL", "worker")
            failed = wait_task(api, task_id, {"failed"}, timeout=100)
            assert failed["error"]["code"] == "TASK_LEASE_EXPIRED"
            with Session(connections.engine) as db:
                successor = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id))
                assert successor.root_task_id == task_id
                successor_id = successor.id
            assert not api.get(f"/api/v1/model-runs/{run_id}").json()["registered"]
        finally:
            docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
        wait_ready()
        wait_task(api, successor_id, {"succeeded"}, timeout=90)
        result = api.get(f"/api/v1/model-runs/{run_id}").json()
        assert result["registered"] and result["task_id"] == successor_id
        assert api.get(f"/api/v1/model-runs/{run_id}?artifact=report.json").status_code == 200
        assert not result["candidate_release"]
        with Session(connections.engine) as db:
            assert db.get(ModelRun, run_id).output_json["attempt_id"] != old_token
