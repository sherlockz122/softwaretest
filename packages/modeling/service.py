"""Model task fences and supervised fit; artifacts register only in the last transaction."""

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from uuid import UUID, uuid4

import psutil
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.auth.security import require_role
from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.modeling import data, policy
from packages.modeling.policy import ModelError
from packages.persistence.models import AsyncTask, ModelArtifact, ModelRun
from packages.tasks.service import TaskService, audit, database_now, key_valid

# 风险列表直接读训练已写出的 probabilities.csv，不另存一份分数。读取必须有界：
# 行数超限或工件体积超限都明确报错，静默截断会让人以为已经看到全部高风险提交。
RISK_ROW_LIMIT = 20000
RISK_BYTE_LIMIT = 16 * 1024**2
RISK_EVAL_PARTITIONS = ("validation", "test")
PROBABILITY_HEADER = (
    "partition",
    "canonical_repository",
    "commit_id",
    "event_epoch",
    "label",
    "probability",
)


def risk_level(probability, threshold):
    """按该次运行自己的阈值分三档；阈值缺失时不臆造等级。"""
    if type(threshold) not in (int, float):
        return None
    if probability >= threshold:
        return "high"
    if probability >= threshold / 2:
        return "medium"
    return "low"


def risk_rows(reader, partitions, limit=RISK_ROW_LIMIT):
    """解析逐提交概率，丢掉标签列，只保留可对外展示的字段。

    传入的是 csv.DictReader，读文件的工作留给调用方，逻辑本身不碰磁盘。
    """
    if tuple(reader.fieldnames or ()) != PROBABILITY_HEADER:
        raise ModelError("MODEL_ARTIFACT_DRIFT", 409)
    rows = []
    for count, row in enumerate(reader, start=1):
        if count > limit:
            raise ModelError("MODEL_RISK_LIST_LIMIT", 413)
        if row["partition"] not in partitions:
            continue
        try:
            probability = float(row["probability"])
            epoch = int(float(row["event_epoch"]))
        except (TypeError, ValueError):
            raise ModelError("MODEL_ARTIFACT_DRIFT", 409) from None
        if not 0.0 <= probability <= 1.0:
            raise ModelError("MODEL_ARTIFACT_DRIFT", 409)
        rows.append(
            {
                "commit_id": row["commit_id"],
                "event_epoch": epoch,
                "partition": row["partition"],
                "probability": probability,
            }
        )
    return rows


def risk_page(rows, page, page_size, threshold, partitions):
    """概率降序、同概率按提交编号升序，保证同一份工件每次顺序一致。"""
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise ModelError("MODEL_INVALID_INPUT")
    ordered = sorted(rows, key=lambda item: (-item["probability"], item["commit_id"]))
    start = (page - 1) * page_size
    items = [dict(item, risk_level=risk_level(item["probability"], threshold))
             for item in ordered[start : start + page_size]]
    return {
        "items": items,
        "total": len(ordered),
        "page": page,
        "page_size": page_size,
        "partitions": list(partitions),
        "threshold": threshold,
        "in_sample": "train" in partitions,
    }


def task_request(payload):
    # Legacy registered requests stay readable; new tasks avoid MySQL JSON doubles.
    if "request_json" in payload:
        return json.loads(payload["request_json"])
    return payload["request"]


def root_request(run):
    return {
        "dataset_id": run.dataset_id,
        "plugin": run.plugin,
        "params": run.params_json,
        "seed": run.seed,
    }


class ModelStopped(Exception):
    pass


class ModelService:
    def __init__(self, settings, connections, *, output=None, datasets=None):
        self.settings, self.engine = settings, connections.engine
        self.tasks = TaskService(settings, connections)
        self.output, self.datasets = output or policy.OUTPUT, datasets

    def create(self, user, key, request, request_id):
        key_valid(key)
        request = request | {
            "params": policy.parameters(request["plugin"], request["params"], request["seed"])
        }
        scope = user.id + ":model.train"

        def replay(db):
            row = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "model.train",
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            if row and task_request(row.payload) != request:
                raise ModelError("TASK_IDEMPOTENCY_CONFLICT", 409)
            return (
                (self.tasks.acknowledgement(row) | {"model_run_id": row.root_task_id})
                if row
                else None
            )

        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if result := replay(db):
                    return result
                snapshot = {
                    "data": data.reference(db, request["dataset_id"]),
                    "provenance": policy.provenance(),
                }
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {
                        "model_version": policy.VERSION,
                        "request_json": assets.canonical(request).decode("utf-8"),
                    },
                    scope,
                    request_id,
                    kind="model.train",
                )
                db.add(
                    ModelRun(
                        id=task.id,
                        latest_task_id=task.id,
                        dataset_id=request["dataset_id"],
                        plugin=request["plugin"],
                        params_json=request["params"],
                        seed=request["seed"],
                        code_sha=snapshot["provenance"]["code_sha"],
                        snapshot_json=snapshot,
                    )
                )
                db.flush()
                return self.tasks.acknowledgement(task) | {"model_run_id": task.id}
        except IntegrityError as exc:
            if exc.orig.args[0] != 1062:
                raise
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if result := replay(db):
                    return result
            raise

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "model.train"
            or task.execution_token != token
            or task.status not in {"running", "cancel_requested"}
            or task.lease_until is None
            or task.lease_until <= now
        ):
            return None
        if task.status == "cancel_requested":
            self.tasks.terminal(db, task, "cancelled")
            audit(db, task, "task.cancelled")
            return None
        run = db.get(ModelRun, task.root_task_id, with_for_update=True)
        if not run or run.latest_task_id != task.id or run.output_json is not None:
            return None
        if task.payload.get("model_version") != policy.VERSION:
            self.tasks.terminal(db, task, "failed", "MODEL_VERSION_CONFLICT")
            audit(db, task, "model.failed")
            return None
        return task, run, now

    def heartbeat(self, task_id, token, stage="verifying_dataset"):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, _, now = context
            stages = {
                "verifying_dataset": 5,
                "fitting_train": 20,
                "calibrating_validation": 55,
                "evaluating_test": 70,
                "artifacts_complete": 90,
                "verifying_artifacts": 95,
            }
            task.stage, task.progress = stage, stages.get(stage, 5)
            task.heartbeat_at, task.lease_until = (
                now,
                now + timedelta(seconds=self.settings.task_lease_seconds),
            )
            task.version += 1
            return True

    def publish(self, task_id, token, output, report):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, run, _ = context
            if (
                output["model_run_id"] != run.id
                or output["attempt_id"] != token
                or output["snapshot"] != run.snapshot_json
                or report["request"] != task_request(task.payload)
                or report["request"] != root_request(run)
                or report["provenance"] != run.snapshot_json["provenance"]
            ):
                raise ModelError("MODEL_ARTIFACT_DRIFT", 409)
            if data.reference(db, run.dataset_id) != run.snapshot_json["data"]:
                raise ModelError("MODEL_DATA_DRIFT", 409)
            run.metrics_json, run.output_json = report, output
            for name, record in output["files"].items():
                db.add(
                    ModelArtifact(
                        id=str(uuid4()),
                        model_run_id=run.id,
                        kind=name,
                        uri=f"{run.id}/{token}/{name}",
                        sha256=record["sha256"],
                        size=record["bytes"],
                    )
                )
            task.result_json = {
                "model_run_id": run.id,
                "plugin": run.plugin,
                "manifest_hash": output["content_hash"],
                "candidate_release": False,
            }
            task.progress = 100
            self.tasks.terminal(db, task, "succeeded")
            audit(db, task, "model.register")
            db.flush()
            return True

    def run(self, task_id, token, payload, *, observe=None):
        process, started, last, last_capacity = None, time.monotonic(), 0, 0
        stage = "verifying_dataset"

        def check():
            nonlocal last, last_capacity
            now = time.monotonic()
            if now - started > policy.MAX_SECONDS:
                raise ModelError("MODEL_DEADLINE")
            if now - last_capacity >= 2:
                root = assets.safe_path(self.output)
                if shutil.disk_usage(root if root.exists() else root.parent).free < 2.5 * 1024**3:
                    raise ModelError("MODEL_CAPACITY_UNSAFE")
                last_capacity = now
            if now - last >= min(2, self.settings.task_heartbeat_seconds):
                if not self.heartbeat(task_id, token, stage):
                    raise ModelStopped
                last = now

        try:
            check()
            with Session(self.engine) as db:
                run = db.get(ModelRun, self.tasks.detail(task_id).get("model_run_id"))
                frozen = run.snapshot_json
                run_id = run.id
                request = task_request(payload)
                if request != root_request(run):
                    raise ModelError("MODEL_ARTIFACT_DRIFT")
            if frozen["provenance"] != policy.provenance():
                raise ModelError("MODEL_VERSION_CONFLICT")
            directory = assets.safe_path(self.output / str(UUID(run_id)) / str(UUID(token)))
            directory.mkdir(parents=True, mode=0o700, exist_ok=False)
            data.audit_dataset(
                self.engine, frozen["data"], directory, check, source_root=self.datasets
            )
            assets.write_json(directory / "request.json", request)
            environment = {k: v for k, v in os.environ.items() if not k.startswith("DG_")}
            environment.update(
                {
                    k: "1"
                    for k in (
                        "OMP_NUM_THREADS",
                        "OPENBLAS_NUM_THREADS",
                        "MKL_NUM_THREADS",
                        "NUMEXPR_NUM_THREADS",
                    )
                }
            )
            with (directory / "process.log").open("xb") as log:
                parent = psutil.Process()
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "packages.modeling.train",
                        str(directory),
                        str(parent.pid),
                        str(parent.create_time()),
                    ],
                    stdout=log,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                maximum = 0
                while process.poll() is None:
                    check()
                    try:
                        current = psutil.Process(process.pid)
                        rss = current.memory_info().rss + sum(
                            c.memory_info().rss for c in current.children(recursive=True)
                        )
                        maximum = max(maximum, rss)
                        if rss > policy.MAX_RSS:
                            raise ModelError("MODEL_MEMORY_LIMIT")
                    except (psutil.NoSuchProcess, psutil.ZombieProcess):
                        pass
                    status = directory / "stage.txt"
                    if status.is_file() and status.stat().st_size <= 64:
                        value = status.read_text(encoding="ascii")
                        if value in {
                            "fitting_train",
                            "calibrating_validation",
                            "evaluating_test",
                            "artifacts_complete",
                        }:
                            stage = value
                    if observe:
                        observe(process, stage)
                    time.sleep(0.5)
                if process.returncode:
                    error = directory / "error.txt"
                    value = (
                        error.read_text(encoding="ascii")
                        if error.is_file() and error.stat().st_size <= 80
                        else ""
                    )
                    known = {
                        "MODEL_CALIBRATION_UNUSABLE",
                        "MODEL_ARTIFACT_UNTRUSTED",
                        "MODEL_PERSISTENCE_DRIFT",
                        "MODEL_NOT_CONVERGED",
                        "MODEL_DATA_INVALID",
                        "MODEL_DATA_LIMIT",
                    }
                    raise ModelError(value if value in known else "MODEL_TRAINING_FAILED")
            stage = "verifying_artifacts"
            check()
            report = assets.read_json(directory / "report.json", check, maximum=2 * 1024**2)
            if report["request"] != request or report["provenance"] != frozen["provenance"]:
                raise ModelError("MODEL_ARTIFACT_DRIFT")
            records = {name: assets.file_digest(directory / name, check) for name in policy.FILES}
            output = assets.sealed(
                {
                    "model_run_id": run_id,
                    "attempt_id": token,
                    "snapshot": frozen,
                    "files": records,
                    "peak_child_rss_bytes": maximum,
                }
            )
            assets.write_json(directory / "manifest.json", output)
            data.audit_dataset(self.engine, frozen["data"], None, check, source_root=self.datasets)
            self.publish(task_id, token, output, report)
        except ModelStopped:
            pass
        except (ModelError, assets.AssetError, OSError, ValueError, KeyError, TypeError) as exc:
            with Session(self.engine) as db, db.begin():
                context = self.fenced(db, task_id, token)
                if context:
                    task, _, _ = context
                    self.tasks.terminal(
                        db,
                        task,
                        "failed",
                        exc.code
                        if isinstance(exc, ModelError)
                        else "MODEL_DATA_OR_STORAGE_INVALID",
                    )
                    audit(db, task, "model.failed")
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    @staticmethod
    def visible(run, task):
        ready = run.output_json is not None and task.status == "succeeded"
        return {
            "id": run.id,
            "dataset_id": run.dataset_id,
            "plugin": run.plugin,
            "params": run.params_json,
            "seed": run.seed,
            "status": task.status,
            "task_id": task.id,
            "registered": ready,
            "candidate_release": False,
            "created_at": run.created_at.isoformat() + "Z",
            "manifest_hash": run.output_json["content_hash"] if ready else None,
        }

    def listing(self, page=1, page_size=20):
        with Session(self.engine) as db:
            total = db.scalar(select(func.count()).select_from(ModelRun))
            rows = db.execute(
                select(ModelRun, AsyncTask)
                .join(AsyncTask, ModelRun.latest_task_id == AsyncTask.id)
                .order_by(ModelRun.created_at.desc(), ModelRun.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [self.visible(r, t) for r, t in rows],
                "plugins": policy.PLUGINS,
                "production_models": [],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def detail(self, run_id):
        with Session(self.engine) as db:
            run = db.get(ModelRun, run_id)
            if not run:
                raise ModelError("MODEL_NOT_FOUND", 404)
            result = self.visible(run, db.get(AsyncTask, run.latest_task_id))
            result.update(
                snapshot=run.snapshot_json,
                report=run.metrics_json if result["registered"] else None,
                artifacts=run.output_json["files"] if result["registered"] else None,
            )
            return result

    def artifact(self, run_id, kind):
        """定位并校验已发布工件，返回（路径, 工件记录, 运行报告）。"""
        if kind not in policy.FILES:
            raise ModelError("MODEL_INVALID_INPUT")
        with Session(self.engine) as db:
            run = db.get(ModelRun, run_id)
            if not run:
                raise ModelError("MODEL_NOT_FOUND", 404)
            if not self.visible(run, db.get(AsyncTask, run.latest_task_id))["registered"]:
                raise ModelError("MODEL_NOT_READY", 409)
            output = run.output_json
            record = db.scalar(
                select(ModelArtifact).where(
                    ModelArtifact.model_run_id == run_id, ModelArtifact.kind == kind
                )
            )
            if (
                not record
                or {"bytes": record.size, "sha256": record.sha256} != output["files"][kind]
            ):
                raise ModelError("MODEL_ARTIFACT_DRIFT", 409)
            if record.uri != f"{run_id}/{output['attempt_id']}/{kind}":
                raise ModelError("MODEL_ARTIFACT_DRIFT", 409)
            report = run.metrics_json or {}
        assets.verify_seal(output)
        directory = assets.safe_path(
            self.output / str(UUID(run_id)) / str(UUID(output["attempt_id"]))
        )
        budget = assets.Budget(seconds=30)
        if assets.read_json(directory / "manifest.json", budget.check) != output:
            raise ModelError("MODEL_ARTIFACT_DRIFT", 409)
        path = assets.safe_path(directory / kind)
        if assets.file_digest(path, budget.check) != output["files"][kind]:
            raise ModelError("MODEL_ARTIFACT_DRIFT", 409)
        return path, record, report

    def download(self, run_id, kind):
        return self.artifact(run_id, kind)[0]

    def risk_items(self, run_id, page=1, page_size=20, partitions=None):
        """历史风险列表：默认只看验证与测试分区，训练分区要显式请求。"""
        chosen = tuple(partitions) if partitions else RISK_EVAL_PARTITIONS
        if (
            not chosen
            or len(set(chosen)) != len(chosen)
            or any(item not in research.PARTITIONS for item in chosen)
        ):
            raise ModelError("MODEL_INVALID_INPUT")
        path, record, report = self.artifact(run_id, "probabilities.csv")
        if record.size > RISK_BYTE_LIMIT:
            raise ModelError("MODEL_RISK_LIST_LIMIT", 413)
        with path.open(encoding="utf-8", newline="") as stream:
            rows = risk_rows(csv.DictReader(stream), chosen)
        return risk_page(rows, page, page_size, report.get("threshold"), chosen)

    def release(self, user, run_id):
        require_role(user, "Admin")
        self.detail(run_id)
        raise ModelError("MODEL_PURPOSE_INCOMPATIBLE", 409)
