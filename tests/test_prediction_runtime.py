# ruff: noqa: F811
"""E2E-02: registered model over a real repository drives review prediction and queue."""

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.mining.features import FEATURE_VERSION
from packages.persistence.models import Repository
from packages.tasks.service import TaskService
from tests.dataset_support import fixture_asset
from tests.repository_support import git
from tests.szz_support import szz_history
from tests.test_runtime import docker
from tests.test_sync_runtime import seed
from tests.test_tasks_runtime import runtime_client, wait_task

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1",
        reason="explicit live E2E opt-in required",
    ),
]

LIMIT = 200


def diff_with_files(count):
    blocks = []
    for index in range(count):
        blocks.append(
            f"diff --git a/f{index}.py b/f{index}.py\n"
            f"--- a/f{index}.py\n"
            f"+++ b/f{index}.py\n"
            "@@ -0,0 +1 @@\n"
            "+line"
        )
    return "\n".join(blocks)


def single_diff():
    return (
        "diff --git a/code.py b/code.py\n"
        "--- a/code.py\n"
        "+++ b/code.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-value = 0\n"
        "+value = 1"
    )


def assert_prediction_fields(body):
    assert 0 <= body["probability"] <= 1
    assert body["risk_level"] in {"high", "medium", "low"}
    assert isinstance(body["effort"], int) and body["effort"] >= 0
    assert 0 <= body["risk_density"] <= 1
    assert 0 < body["threshold"] < 1
    assert body["feature_version"] == FEATURE_VERSION
    assert body["calibration"]
    assert isinstance(body["top_factors"], list)
    assert len(body["limitations"]) >= 1


def test_e2e02_candidate_batch_explanation_queue_and_error_paths(tmp_path):
    # 1. 真实仓库：基线标签和候选分支在克隆前打好，贯穿解析/修复/特征到模型。
    source, _, _, shas = szz_history(tmp_path / "source", count=120)
    git("-C", str(source), "tag", "v1.0", shas[40])
    git("-C", str(source), "branch", "review/candidate", shas[100])
    bare = tmp_path / "fixture.git"
    git("clone", "--bare", str(source), str(bare))
    repo_id = str(uuid4())
    key = seed(bare, repo_id)

    root = assets.ROOT / "runtime/temp" / ("prediction-runtime-" + uuid4().hex)
    root.mkdir(parents=True)
    directory, imported = fixture_asset(root, rows=4000, store=assets.ROOT / "runtime/public-assets")

    with runtime_client() as (api, settings, connections):
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
                    canonical_url=f"https://fixture.example/predictions/{repo_id}",
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
            wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=240)
        response = api.post(
            path + "/feature-runs",
            json={"feature_version": FEATURE_VERSION, "fix_run_id": response.json()["task_id"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202
        wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=240)

        # 2. 已注册模型：公开数据训练 LR，产出校准器与判定阈值工件。
        response = api.post(
            "/api/v1/datasets",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "name": "prediction review fixture",
                "asset_id": directory.name,
                "source_hash": imported["manifest"]["content_hash"],
            },
        )
        assert response.status_code == 202
        dataset_id = response.json()["dataset_id"]
        wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=120)
        response = api.post(
            "/api/v1/model-runs",
            headers={"Idempotency-Key": uuid4().hex},
            json={"dataset_id": dataset_id, "plugin": "lr", "params": {}, "seed": 42},
        )
        assert response.status_code == 202
        run_id = response.json()["model_run_id"]
        wait_task(api, response.json()["task_id"], {"succeeded"}, timeout=180)

        # 3. 候选引用预测：同步返回结果或超时转任务，两种口径都按契约校验。
        candidate_key = uuid4().hex
        response = api.post(
            "/api/v1/predictions/candidate",
            headers={"Idempotency-Key": candidate_key},
            json={
                "repository_id": repo_id,
                "model_run_id": run_id,
                "base_ref": "v1.0",
                "candidate_ref": "review/candidate",
            },
        )
        assert response.status_code == 202, response.text
        first = response.json()
        if "prediction_id" in first:
            prediction_id = first["prediction_id"]
            assert_prediction_fields(first)
        else:
            assert first["status"] == "queued"
            wait_task(api, first["task_id"], {"succeeded"}, timeout=120)
            queue = api.get(f"/api/v1/prediction-tasks/{first['task_id']}/results").json()
            assert queue["total"] == 1
            prediction_id = queue["items"][0]["prediction_id"]
            item = queue["items"][0]
            assert item["candidate_sha"] == shas[100]
            # 队列项按规格显示审查成本、风险密度和显著标识，阈值与特征版本在队列顶层。
            for field in (
                "probability",
                "risk_level",
                "effort",
                "risk_density",
                "oversized",
                "top_factors",
            ):
                assert field in item
            assert queue["threshold"] is not None
            assert queue["feature_version"] == FEATURE_VERSION

        # 4. 解释：因素按贡献绝对值降序，携带方法和非因果声明。
        explanation = api.get(f"/api/v1/predictions/{prediction_id}/explanation")
        assert explanation.status_code == 200
        body = explanation.json()
        assert body["method"] == "lr-scaled-coefficient-v1"
        assert body["factors"] and all(
            {"feature", "value", "contribution", "direction"} <= set(factor)
            for factor in body["factors"]
        )
        magnitudes = [abs(factor["contribution"]) for factor in body["factors"]]
        assert magnitudes == sorted(magnitudes, reverse=True)
        assert len(body["limitations"]) >= 1
        assert body["model_run_id"] == run_id and body["feature_version"] == FEATURE_VERSION

        # 5. 直接提交改动内容：与引用路径同口径。
        response = api.post(
            "/api/v1/predictions/candidate",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "repository_id": repo_id,
                "model_run_id": run_id,
                "base_ref": "v1.0",
                "candidate_diff": single_diff(),
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        if "prediction_id" in body:
            assert_prediction_fields(body)
        else:
            wait_task(api, body["task_id"], {"succeeded"}, timeout=120)

        # 6. 异常路径：无效引用、基准点不可达、改动超限、引用与基准点相同、模型未注册。
        cases = [
            (
                {"repository_id": repo_id, "model_run_id": run_id, "base_ref": "v1.0", "candidate_ref": "refs/tags/missing"},
                422,
                "PRED_REF_UNRESOLVABLE",
            ),
            (
                {"repository_id": repo_id, "model_run_id": run_id, "base_ref": "refs/tags/missing", "candidate_ref": "review/candidate"},
                422,
                "PRED_BASE_UNREACHABLE",
            ),
            (
                {"repository_id": repo_id, "model_run_id": run_id, "base_ref": "v1.0", "candidate_diff": diff_with_files(LIMIT + 1)},
                422,
                "PRED_INPUT_LIMIT",
            ),
            (
                {"repository_id": repo_id, "model_run_id": run_id, "base_ref": "v1.0", "candidate_ref": "v1.0"},
                422,
                "PRED_INVALID_INPUT",
            ),
            (
                {"repository_id": repo_id, "model_run_id": str(uuid4()), "base_ref": "v1.0", "candidate_ref": "review/candidate"},
                404,
                "PRED_MODEL_NOT_FOUND",
            ),
        ]
        for body, status, code in cases:
            response = api.post(
                "/api/v1/predictions/candidate",
                headers={"Idempotency-Key": uuid4().hex},
                json=body,
            )
            assert response.status_code == status, response.text
            assert response.json()["code"] == code

        # 7. 批量预测：任务完成后队列按概率降序，筛选和超大标识可用。
        response = api.post(
            "/api/v1/prediction-tasks",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "repository_id": repo_id,
                "model_run_id": run_id,
                "base_ref": "v1.0",
                "window_days": 90,
                "max_candidates": 30,
            },
        )
        assert response.status_code == 202, response.text
        batch = response.json()
        wait_task(api, batch["task_id"], {"succeeded"}, timeout=300)
        queue = api.get(f"/api/v1/prediction-tasks/{batch['task_id']}/results").json()
        assert queue["total"] == 30 and queue["empty"] is False
        probabilities = [item["probability"] for item in queue["items"]]
        assert probabilities == sorted(probabilities, reverse=True)
        assert queue["threshold"] is not None and queue["feature_version"] == FEATURE_VERSION
        assert queue["candidate_kind"] == "batch"
        for item in queue["items"]:
            assert {"probability", "risk_level", "effort", "risk_density", "oversized", "top_factors"} <= set(item)
        filtered = api.get(
            f"/api/v1/prediction-tasks/{batch['task_id']}/results",
            params={"level": "high", "oversized": "false"},
        ).json()
        assert all(item["risk_level"] == "high" and not item["oversized"] for item in filtered["items"])
        assert filtered["total"] <= queue["total"]

        # 8. 同键重放返回原响应，同键异载荷返回冲突。
        replay = api.post(
            "/api/v1/predictions/candidate",
            headers={"Idempotency-Key": candidate_key},
            json={
                "repository_id": repo_id,
                "model_run_id": run_id,
                "base_ref": "v1.0",
                "candidate_ref": "review/candidate",
            },
        )
        assert replay.status_code == 202
        if "prediction_id" in first:
            assert replay.json() == first
        else:
            # 异步路径重放时任务已完成，返回完整预测或原任务引用都指向同一结果。
            assert (
                replay.json().get("task_id") == first["task_id"]
                or "prediction_id" in replay.json()
            )
        conflict = api.post(
            "/api/v1/predictions/candidate",
            headers={"Idempotency-Key": candidate_key},
            json={
                "repository_id": repo_id,
                "model_run_id": run_id,
                "base_ref": "v1.0",
                "candidate_ref": "main",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "TASK_IDEMPOTENCY_CONFLICT"
