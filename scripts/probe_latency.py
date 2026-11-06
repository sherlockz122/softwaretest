"""在线接口延迟压测：只测在线请求，不测后台任务耗时。

用法示例见 docs/reports 下对应的压测报告。脚本把每次请求写成一行记录，
汇总只按有效请求计算分位数，并把环境、数据量与模型版本一并固定下来。
"""

import argparse
import csv
import json
import math
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx

REQUESTS_LIMIT = 2000
VALID_MINIMUM = 100
COLUMNS = (
    "target",
    "group",
    "phase",
    "seq",
    "started_at",
    "elapsed_ms",
    "status",
    "valid",
    "reason",
)


@dataclass(frozen=True)
class Target:
    """一个被测接口：名称、路径模板与该接口必须具备的关键字段。"""

    name: str
    path: str
    required: tuple


TARGETS = (
    Target("models.list", "/api/v1/models?page=1&page_size=20", ("items", "total")),
    Target("datasets.list", "/api/v1/datasets?page=1&page_size=20", ("items", "total")),
    Target("tasks.list", "/api/v1/tasks?page=1&page_size=20", ("items", "total")),
    Target("repositories.list", "/api/v1/repositories?page=1&page_size=20", ("items", "total")),
    Target("model-runs.detail", "/api/v1/model-runs/{run_id}", ("id", "dataset_id", "registered")),
    Target(
        "risk-items.page",
        "/api/v1/model-runs/{run_id}/risk-items?page=1&page_size=20",
        ("items", "total", "threshold", "partitions"),
    ),
    Target(
        "risk-items.max-page",
        "/api/v1/model-runs/{run_id}/risk-items?page=1&page_size=100",
        ("items", "total", "page_size"),
    ),
)

# 验收计划点名要测的两条链路。候选预测在本次改动时还没有路由，脚本照样去探一次，
# 把真实状态码记进汇总，避免报告里出现"没测"与"不存在"混为一谈。
PREDICTED_ABSENT = (
    ("candidate-prediction.review", "/api/v1/review/predictions"),
    ("candidate-prediction.web-route", "/api/v1/predictions"),
)


def percentile(samples, fraction):
    """最近秩法分位数；样本为空时抛错，不做任何填充。"""
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("percentile of empty sample")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def classify(status, payload, required):
    """有效请求判定：状态码、JSON 结构与关键字段三道关，返回（是否有效，原因）。"""
    if status != 200:
        return False, f"http_{status}"
    if not isinstance(payload, dict):
        return False, "body_not_object"
    missing = [key for key in required if key not in payload]
    if missing:
        return False, "missing:" + ",".join(missing)
    return True, ""


def summarize(records, *, minimum=VALID_MINIMUM):
    """按目标与并发方式分组汇总；分位数只吃有效样本。"""
    groups = {}
    for record in records:
        if record["phase"] != "measure":
            continue
        key = (record["target"], record["group"])
        groups.setdefault(key, []).append(record)
    summary = []
    for (target, group), items in sorted(groups.items()):
        elapsed = [item["elapsed_ms"] for item in items if item["valid"]]
        wall = max(item["finished_at"] for item in items) - min(
            item["started_at_clock"] for item in items
        )
        entry = {
            "target": target,
            "group": group,
            "requests": len(items),
            "valid": len(elapsed),
            "invalid": len(items) - len(elapsed),
            "error_rate": round((len(items) - len(elapsed)) / len(items), 4) if items else None,
            "wall_seconds": round(wall, 3),
            "throughput_per_second": round(len(elapsed) / wall, 2) if wall > 0 else None,
            "p50_ms": round(percentile(elapsed, 0.50), 2) if elapsed else None,
            "p95_ms": round(percentile(elapsed, 0.95), 2) if elapsed else None,
            "p99_ms": round(percentile(elapsed, 0.99), 2) if elapsed else None,
            "meets_minimum": len(elapsed) >= minimum,
        }
        summary.append(entry)
    return summary


def environment():
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    try:
        import psutil

        info["cpu_logical"] = psutil.cpu_count(logical=True)
        info["cpu_physical"] = psutil.cpu_count(logical=False)
        info["memory_gib"] = round(psutil.virtual_memory().total / 1024**3, 1)
    except Exception:
        info["cpu_logical"], info["cpu_physical"], info["memory_gib"] = None, None, None
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        )
        info["git_commit"] = commit.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        info["git_commit"] = None
    return info


def login(client, username, password):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def detail(client, run_id):
    payload = client.get(f"/api/v1/model-runs/{run_id}").json()
    dataset = client.get(f"/api/v1/datasets/{payload['dataset_id']}").json()
    risk = client.get(f"/api/v1/model-runs/{run_id}/risk-items?page_size=1").json()
    return {
        "model": {
            "run_id": payload["id"],
            "plugin": payload["plugin"],
            "params": payload["params"],
            "seed": payload["seed"],
            "manifest_hash": payload["manifest_hash"],
            "threshold": risk.get("threshold"),
            "registered": payload["registered"],
            "candidate_release": payload["candidate_release"],
        },
        "data": {
            "dataset_id": dataset["id"],
            "dataset_rows": dataset["total"],
            "processed": dataset["processed"],
            "risk_items": risk.get("total"),
            "risk_partitions": risk.get("partitions"),
        },
    }


def probe(client, target, run_id, required_keys, group, count, warmup, records):
    """对单个接口发 count 次计量请求，预热轮次单独标记。"""
    url = target.path.format(run_id=run_id)

    def once(phase, seq):
        started_at = time.time()
        clock = time.perf_counter()
        try:
            response = client.get(url)
            elapsed = (time.perf_counter() - clock) * 1000
            status = response.status_code
            try:
                payload = response.json()
            except ValueError:
                payload = None
            valid, reason = classify(status, payload, required_keys)
        except httpx.HTTPError as error:
            elapsed = (time.perf_counter() - clock) * 1000
            status, valid, reason = 0, False, type(error).__name__
        records.append(
            {
                "target": target.name,
                "group": group,
                "phase": phase,
                "seq": seq,
                "started_at": started_at,
                "started_at_clock": clock,
                "finished_at": time.perf_counter(),
                "elapsed_ms": round(elapsed, 3),
                "status": status,
                "valid": valid,
                "reason": reason,
            }
        )

    for seq in range(1, warmup + 1):
        once("warmup", seq)
    if group == "serial":
        for seq in range(1, count + 1):
            once("measure", seq)
    else:
        workers = int(group.split("-")[1])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda seq: once("measure", seq), range(1, count + 1)))


def write_csv(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    **record,
                    "started_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.localtime(record["started_at"])
                    ),
                    "elapsed_ms": f"{record['elapsed_ms']:.3f}",
                }
            )


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="DefectGuard 在线延迟压测")
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--username", default=os.environ.get("DG_BOOTSTRAP_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("DG_BOOTSTRAP_PASSWORD", ""))
    parser.add_argument("--run-id", default="", help="模型运行标识；风险列表与运行详情需要它")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument(
        "--concurrency", type=int, action="append", default=None, help="可重复；1 表示串行"
    )
    parser.add_argument("--minimum", type=int, default=VALID_MINIMUM)
    parser.add_argument("--csv", default="probe/out/latency_raw.csv")
    parser.add_argument("--json", default="probe/out/latency_summary.json")
    args = parser.parse_args(argv)

    groups = args.concurrency or [1]
    if args.requests > REQUESTS_LIMIT:
        parser.error(f"requests must not exceed {REQUESTS_LIMIT}")
    if not args.username or not args.password:
        parser.error("supply --username/--password or DG_BOOTSTRAP_* environment variables")

    records = []
    absent = []
    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        client.headers["Authorization"] = "Bearer " + login(client, args.username, args.password)
        fixed = detail(client, args.run_id) if args.run_id else {"model": None, "data": None}
        if args.run_id:
            targets = TARGETS
        else:
            targets = tuple(t for t in TARGETS if "{run_id}" not in t.path)
        for name, path in PREDICTED_ABSENT:
            response = client.get(path)
            absent.append({"target": name, "path": path, "status": response.status_code})
        for group in groups:
            label = "serial" if group == 1 else f"concurrency-{group}"
            for target in targets:
                print(f"{label} {target.name} x{args.requests} ...", flush=True)
                probe(
                    client,
                    target,
                    args.run_id,
                    target.required,
                    label,
                    args.requests,
                    args.warmup,
                    records,
                )

    summary = summarize(records, minimum=args.minimum)
    write_csv(Path(args.csv), records)
    write_json(
        Path(args.json),
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "base_url": args.base_url,
            "requests_per_target": args.requests,
            "warmup_per_target": args.warmup,
            "minimum_valid": args.minimum,
            "environment": environment(),
            "model": fixed["model"],
            "data": fixed["data"],
            "groups": summary,
            "absent_targets": absent,
            "raw_csv": args.csv,
        },
    )
    short = [entry for entry in summary if not entry["meets_minimum"] or not entry["p95_ms"]]
    for entry in summary:
        print(
            f"{entry['group']:>14} {entry['target']:<32} "
            f"n={entry['valid']:<4} p50={entry['p50_ms']} p95={entry['p95_ms']} "
            f"p99={entry['p99_ms']} err={entry['error_rate']}",
            flush=True,
        )
    if short:
        print("有效请求不足或没有有效样本，见上面的分组", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
