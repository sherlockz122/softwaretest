"""建一份固定的演示数据集与一次模型运行，供在线压测与浏览器实测使用。

数据集来自仓库自带的工程夹具，标签沿一条线性边界生成，只用于演示延迟与页面，
不用于主张模型效果。账号口令从 DG_BOOTSTRAP_USERNAME 与 DG_BOOTSTRAP_PASSWORD 读。
"""

import argparse
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from packages.mining import public_assets as assets
from packages.mining.public_dataset import NUMERIC_FIELDS
from tests.dataset_support import fixture_asset

YEAR_2003_EPOCH = 1_041_379_200


def mutate(row, index, project):
    """让十二个数值列彼此不同，并让标签沿一条树模型能学到的边界生成。

    夹具默认把每个数值列都写成行号，模型没有可分的信号，概率会全部挤在 0.5。
    """
    values = {
        name: float((index * (position * 2 + 3)) % 37 + position % 5)
        for position, name in enumerate(NUMERIC_FIELDS)
    }
    buggy = "True" if values["ns"] + values["nf"] > 20 else "False"
    row["buggy"] = buggy
    row["fix"] = buggy
    row.update({name: f"{value:.2f}" for name, value in values.items()})
    # 夹具的年份列固定为 2003，日期必须落在同一年，否则两份来源表对不上。
    epoch = YEAR_2003_EPOCH + index * 3600
    row["author_date"] = str(epoch)
    assert datetime.fromtimestamp(epoch, UTC).year == 2003


def wait_task(api, task_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = api.get(f"/tasks/{task_id}").json()
        if payload.get("status") in {"succeeded", "failed", "canceled"}:
            return payload
        time.sleep(1)
    raise SystemExit(f"task {task_id} did not settle in {timeout} seconds")


def build(api, args):
    root = assets.ROOT / "runtime/temp" / args.fixture_dir
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    directory, imported = fixture_asset(
        root, rows=args.rows, mutate=mutate, store=assets.ROOT / "runtime/public-assets"
    )
    created = api.post(
        "/datasets",
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "name": args.dataset_name,
            "asset_id": directory.name,
            "source_hash": imported["manifest"]["content_hash"],
        },
    )
    created.raise_for_status()
    dataset = created.json()
    settled = wait_task(api, dataset["task_id"], args.timeout)
    print(f"dataset {dataset['dataset_id']} {settled['status']}", flush=True)
    if settled["status"] != "succeeded":
        raise SystemExit(settled.get("error"))

    run = api.post(
        "/model-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "dataset_id": dataset["dataset_id"],
            "plugin": args.plugin,
            "params": (
                {"c": 1.0, "max_iter": 500}
                if args.plugin == "lr"
                else {"n_estimators": 120, "max_depth": 16, "min_samples_leaf": 1}
            ),
            "seed": args.seed,
        },
    )
    run.raise_for_status()
    created_run = run.json()
    settled = wait_task(api, created_run["task_id"], args.timeout)
    print(f"model-run {created_run['model_run_id']} {settled['status']}", flush=True)
    if settled["status"] != "succeeded":
        raise SystemExit(settled.get("error"))
    detail = api.get(f"/model-runs/{created_run['model_run_id']}").json()
    risk = api.get(
        f"/model-runs/{created_run['model_run_id']}/risk-items", params={"page_size": 1}
    ).json()
    print(
        f"registered={detail['registered']} candidate_release={detail['candidate_release']} "
        f"risk_items={risk['total']} threshold={risk['threshold']}",
        flush=True,
    )
    print(f"RUN_ID={created_run['model_run_id']}")
    print(f"DATASET_ID={dataset['dataset_id']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="建演示数据集与模型运行")
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--rows", type=int, default=6000)
    parser.add_argument("--plugin", choices=("lr", "rf"), default="rf")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--fixture-dir", default="demo-nfr03")
    parser.add_argument("--dataset-name", default="NFR-03 demo dataset")
    args = parser.parse_args(argv)

    username = os.environ.get("DG_BOOTSTRAP_USERNAME", "")
    password = os.environ.get("DG_BOOTSTRAP_PASSWORD", "")
    if not username or not password:
        parser.error("supply DG_BOOTSTRAP_USERNAME and DG_BOOTSTRAP_PASSWORD")
    with httpx.Client(base_url=args.base_url + "/api/v1", timeout=120, trust_env=False) as api:
        response = api.post("/auth/login", json={"username": username, "password": password})
        response.raise_for_status()
        api.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        build(api, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
