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


def test_feature_choice_training_on_live_stack():
    """真实栈上带特征选择的训练：契约进报告、非法选择在入队前拒绝、缺省保持旧行为。"""
    from packages.mining.public_dataset import NUMERIC_FIELDS

    root = assets.ROOT / "runtime/temp" / ("model-features-" + uuid4().hex)
    root.mkdir(parents=True)
    directory, imported = fixture_asset(
        root, rows=80, store=assets.ROOT / "runtime/public-assets"
    )
    with runtime_client() as (api, settings, connections):
        response = api.post(
            "/api/v1/datasets",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "name": "feature-choice acceptance",
                "asset_id": directory.name,
                "source_hash": imported["manifest"]["content_hash"],
            },
        )
        assert response.status_code == 202
        dataset_id = response.json()["dataset_id"]
        wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=120)
        # 带特征选择：la 取对数、age 标准化（缺省形式按插件补全）
        chosen = api.post(
            "/api/v1/model-runs",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "dataset_id": dataset_id,
                "plugin": "lr",
                "params": {"c": 1, "max_iter": 500},
                "seed": 42,
                "features": ["la", "age"],
                "forms": {"la": "log1p"},
            },
        )
        assert chosen.status_code == 202, chosen.text
        wait_task(api, chosen.json()["task_id"], {"succeeded"}, timeout=180)
        report = api.get("/api/v1/model-runs/" + chosen.json()["model_run_id"]).json()[
            "report"
        ]
        assert report["features"] == ["la", "age"]
        assert report["forms"] == {"la": "log1p", "age": "standardize"}
        assert set(report["standardization"]) == {"age"}
        assert report["request"]["features"] == report["features"]
        # 非法选择在任务创建前拒绝，不产生训练任务
        rejected = api.post(
            "/api/v1/model-runs",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "dataset_id": dataset_id,
                "plugin": "lr",
                "params": {},
                "seed": 42,
                "features": [],
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "MODEL_FEATURE_INVALID"
        # 缺省请求（不带 features/forms）仍是旧行为：全部特征、rf 全原始值
        legacy = api.post(
            "/api/v1/model-runs",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "dataset_id": dataset_id,
                "plugin": "rf",
                "params": {"n_estimators": 256, "max_depth": 32, "min_samples_leaf": 1},
                "seed": 7,
            },
        )
        assert legacy.status_code == 202, legacy.text
        wait_task(api, legacy.json()["task_id"], {"succeeded"}, timeout=180)
        legacy_report = api.get("/api/v1/model-runs/" + legacy.json()["model_run_id"]).json()[
            "report"
        ]
        assert legacy_report["features"] == list(NUMERIC_FIELDS)
        assert legacy_report["forms"] == {name: "raw" for name in NUMERIC_FIELDS}
