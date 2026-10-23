"""Candidate prediction on registered research models; queue, batch and explanation."""

import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import timedelta
from functools import lru_cache
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.mining.feature_inputs import freeze_native_record
from packages.mining.feature_service import decode_input, encode_input
from packages.mining.features import (
    FEATURE_VERSION,
    RULE_HASH,
    CommitInput,
    FeatureInputError,
    FileDelta,
    Snapshot,
    ancestry,
    compute,
    digest,
    vector,
)
from packages.mining.szz import iso
from packages.modeling.service import ModelService
from packages.persistence.models import (
    AsyncTask,
    FeatureItem,
    FeatureRun,
    ModelArtifact,
    ModelRun,
    PredictionResult,
    PredictionTask,
    Repository,
)
from packages.prediction.policy import (
    BATCH_SIZE,
    CALIBRATION,
    COLUMN_FEATURES,
    DISCLAIMER,
    MAX_BATCH_CANDIDATES,
    MAX_CANDIDATE_FILES,
    MAX_CANDIDATE_LINES,
    MAX_DIFF_BYTES,
    MAX_WINDOW_DAYS,
    MODEL_COLUMNS,
    OVERSIZED_EFFORT,
    SYNC_SECONDS,
    VERSION,
    PredictionError,
)
from packages.repositories.clone import CloneExecutor, ExecutionStopped
from packages.repositories.parser import NativeReader
from packages.repositories.safety import RepositoryError
from packages.repositories.service import RepositoryService
from packages.repositories.storage import Storage, safe_path
from packages.tasks.service import TaskService, audit, database_now, key_valid, payload_hash

DIFF_HEADER = re.compile(r"^diff --git ")
HUNK_HEADER = re.compile(r"^@@ ")


class SyncTimeout(Exception):
    pass


def _parse_unified_diff(text):
    """Unified diff to FileDelta rows; hunk-derived counts, no command execution."""
    if (
        not isinstance(text, str)
        or not text
        or len(text.encode("utf-8", "replace")) > MAX_DIFF_BYTES
    ):
        raise PredictionError("PRED_INVALID_INPUT", reason="改动内容为空或超过大小上限")
    blocks, current = [], []
    for line in text.replace("\r\n", "\n").split("\n"):
        if DIFF_HEADER.match(line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    if not blocks:
        raise PredictionError("PRED_INVALID_INPUT", reason="改动内容不含可解析的文件差异")
    deltas, total_lines = [], 0
    for block in blocks:
        header = block[0][len("diff --git ") :]
        old_path = new_path = None
        if header.startswith('"'):
            match = re.match(r'^"a/(.*?)" "b/(.*?)"$', header)
            if match:
                old_path, new_path = match.group(1), match.group(2)
        else:
            match = re.match(r"^a/(.*?) b/(.*)$", header)
            if match:
                old_path, new_path = match.group(1), match.group(2)
        if old_path is None or new_path is None:
            raise PredictionError("PRED_INVALID_INPUT", reason="改动内容含无法解析的文件差异头")
        change_type, old_mode, new_mode = "M", "100644", "100644"
        for line in block[1:]:
            if line.startswith("new file mode"):
                change_type, old_mode = "A", "000000"
            elif line.startswith("deleted file mode"):
                change_type, new_mode = "D", "000000"
            elif line.startswith("rename from "):
                change_type = "R"
                old_path = line[len("rename from ") :]
            elif line.startswith("rename to "):
                change_type = "R"
                new_path = line[len("rename to ") :]
        if change_type == "A":
            old_path = None
        elif change_type == "D":
            new_path = None
        insertions = deletions = context = 0
        in_hunk = False
        for line in block:
            if HUNK_HEADER.match(line):
                in_hunk = True
            elif in_hunk:
                if line.startswith("+++") or line.startswith("---") or line.startswith("\\ "):
                    continue
                if line.startswith("+"):
                    insertions += 1
                elif line.startswith("-"):
                    deletions += 1
                elif line.startswith(" "):
                    context += 1
        try:
            deltas.append(
                FileDelta(
                    old_path=old_path,
                    new_path=new_path,
                    change_type=change_type,
                    insertions=insertions,
                    deletions=deletions,
                    old_loc=context + deletions,
                    old_mode=old_mode,
                    new_mode=new_mode,
                )
            )
        except FeatureInputError as error:
            raise PredictionError(
                "PRED_INVALID_INPUT", reason="改动内容不满足文件改动口径：" + error.code
            ) from None
        total_lines += insertions + deletions
    if len(deltas) > MAX_CANDIDATE_FILES:
        raise PredictionError(
            "PRED_INPUT_LIMIT", reason=f"改动文件数超过上限 {MAX_CANDIDATE_FILES} 个"
        )
    if total_lines > MAX_CANDIDATE_LINES:
        raise PredictionError(
            "PRED_INPUT_LIMIT", reason=f"变更行数超过上限 {MAX_CANDIDATE_LINES} 行"
        )
    return deltas


@lru_cache(maxsize=4)
def _load_bundle(path_text, sha256):
    """Load a verified artifact bundle; cache is keyed by path and content digest."""
    import skops.io as sio

    bundle = sio.load(path_text, trusted=[])
    if not isinstance(bundle, dict) or not {
        "model",
        "calibrator",
        "threshold",
        "features",
    } <= set(bundle):
        raise PredictionError("PRED_MODEL_INELIGIBLE", reason="模型工件缺少校准器或判定阈值")
    if list(bundle["features"]) != MODEL_COLUMNS:
        raise PredictionError(
            "PRED_MODEL_INELIGIBLE", reason="模型工件的特征列合同与系统可重算口径不一致"
        )
    threshold = bundle["threshold"]
    if type(threshold) not in (int, float) or not 0 < threshold < 1:
        raise PredictionError("PRED_MODEL_INELIGIBLE", reason="判定阈值不在合法区间")
    if not hasattr(bundle["model"], "predict_proba"):
        raise PredictionError("PRED_MODEL_INELIGIBLE", reason="模型工件不支持概率推理")
    return bundle


def _level(probability, threshold):
    if probability >= threshold:
        return "high"
    if probability >= threshold / 2:
        return "medium"
    return "low"


def _source_digest(snapshot, purpose):
    """Same source digest formula as the feature kernel's compute()."""
    ordered = sorted(snapshot.commits, key=lambda commit: commit.order)
    return digest(
        {
            "repository_id": snapshot.repository_id,
            "head_sha": snapshot.head_sha,
            "coverage": snapshot.history_coverage,
            "parser_version": snapshot.parser_version,
            "identity_version": snapshot.identity_version,
            "feature_version": FEATURE_VERSION,
            "rule_hash": RULE_HASH,
            "purpose": purpose,
            "commits": [asdict(commit) for commit in ordered],
        }
    )


class PredictionService:
    def __init__(self, settings, connections, *, output=None):
        self.settings, self.engine = settings, connections.engine
        self.tasks = TaskService(settings, connections)
        self.storage = Storage(settings)
        self.output = output
        self.prepared_results = {}
        self._base_sha = self._base_time = self._candidate_sha = None

    # ---- model eligibility ----

    @staticmethod
    def eligible_model(db, run_id):
        run = db.get(ModelRun, run_id)
        if not run:
            raise PredictionError("PRED_MODEL_NOT_FOUND", 404)
        task = db.get(AsyncTask, run.latest_task_id)
        if not task or not ModelService.visible(run, task)["registered"]:
            raise PredictionError("PRED_MODEL_INELIGIBLE", reason="模型运行尚未注册")
        record = db.scalar(
            select(ModelArtifact).where(
                ModelArtifact.model_run_id == run.id, ModelArtifact.kind == "model.skops"
            )
        )
        files = (run.output_json or {}).get("files") or {}
        if (
            not record
            or not files.get("model.skops")
            or {"bytes": record.size, "sha256": record.sha256} != files["model.skops"]
        ):
            raise PredictionError("PRED_MODEL_INELIGIBLE", reason="模型工件注册记录不完整")
        return run

    def load_bundle(self, run):
        path = ModelService(
            self.settings, SimpleNamespace(engine=self.engine), output=self.output
        ).download(run.id, "model.skops")
        return _load_bundle(str(path), run.output_json["files"]["model.skops"]["sha256"])

    # ---- shared context ----

    def _feature_context(self, db, repository_id):
        repo = db.get(Repository, repository_id)
        if not repo:
            raise RepositoryError(404, "REPOSITORY_NOT_FOUND")
        feature_run = (
            db.get(FeatureRun, repo.feature_root_task_id) if repo.feature_root_task_id else None
        )
        if (
            not feature_run
            or feature_run.purpose != "historical"
            or feature_run.feature_version != FEATURE_VERSION
            or feature_run.quality_json is None
        ):
            raise PredictionError(
                "PRED_HISTORY_UNAVAILABLE", reason="仓库尚无完整的历史特征轮次，无法装配候选特征"
            )
        if feature_run.history_coverage != "full":
            raise PredictionError(
                "PRED_HISTORY_UNAVAILABLE", reason="仓库历史只覆盖近期窗口，无法重算模型的全部特征列"
            )
        rows = db.scalars(
            select(FeatureItem)
            .where(FeatureItem.root_task_id == feature_run.root_task_id)
            .order_by(FeatureItem.ordinal)
        ).all()
        frozen = [row.input_json for row in rows]
        if digest(frozen) != feature_run.input_digest:
            raise PredictionError(
                "PRED_HISTORY_UNAVAILABLE", reason="历史特征轮次的冻结输入摘要不一致"
            )
        return repo, feature_run, frozen

    def _bare(self, repo, feature_run):
        path = safe_path(self.storage.root / feature_run.storage_key)
        parts = path.relative_to(self.storage.root).parts
        if (
            len(parts) != 4
            or parts[0] != "objects"
            or parts[1] != repo.id
            or parts[3] != "repo.git"
            or not path.is_dir()
        ):
            raise RepositoryError(503, "REPOSITORY_STORAGE_UNSAFE")
        return path

    @staticmethod
    def _reader(bare, attempt, settings, engine, tick):
        clone = CloneExecutor(settings, RepositoryService(settings, SimpleNamespace(engine=engine)))
        if not clone.git:
            raise RepositoryError(503, "REPOSITORY_GIT_UNAVAILABLE")
        environment, _ = clone.environment(attempt, SimpleNamespace(port=0))
        environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
        configs = {
            "protocol.https.allow": "never",
            "diff.external": "",
            "core.attributesFile": os.devnull,
        }
        start = int(environment["GIT_CONFIG_COUNT"])
        for index, (key, value) in enumerate(configs.items(), start):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        environment["GIT_CONFIG_COUNT"] = str(start + len(configs))
        return NativeReader(
            bare,
            attempt,
            environment,
            tick,
            time.monotonic() + settings.repository_parse_timeout_seconds,
            clone.git,
        )

    @staticmethod
    def _resolve_base(reader, ref):
        peeled = ref + "^{commit}"
        if reader.command(
            ["rev-parse", "--verify", "--end-of-options", peeled], codes=(0, 128), exit_code=True
        ):
            raise PredictionError("PRED_BASE_UNREACHABLE", reason="基准点在仓库中无法解析为提交")
        return (
            reader.command(["rev-parse", "--verify", "--end-of-options", peeled])
            .decode("ascii")
            .strip()
        )

    @staticmethod
    def _check_files(files):
        if len(files) > MAX_CANDIDATE_FILES:
            raise PredictionError(
                "PRED_INPUT_LIMIT", reason=f"改动文件数超过上限 {MAX_CANDIDATE_FILES} 个"
            )
        lines = sum((file["insertions"] or 0) + (file["deletions"] or 0) for file in files)
        if lines > MAX_CANDIDATE_LINES:
            raise PredictionError(
                "PRED_INPUT_LIMIT", reason=f"变更行数超过上限 {MAX_CANDIDATE_LINES} 行"
            )

    @staticmethod
    def _candidate_ref(reader, ref, base_sha):
        peeled = ref + "^{commit}"
        if reader.command(
            ["rev-parse", "--verify", "--end-of-options", peeled], codes=(0, 128), exit_code=True
        ):
            raise PredictionError("PRED_REF_UNRESOLVABLE", reason="候选引用在仓库中无法解析为提交")
        sha = (
            reader.command(["rev-parse", "--verify", "--end-of-options", peeled])
            .decode("ascii")
            .strip()
        )
        if sha == base_sha:
            raise PredictionError("PRED_INVALID_INPUT", reason="候选引用与基准点相同，没有可评审的改动")
        if not reader.ancestor(base_sha, sha):
            raise PredictionError("PRED_BASE_UNREACHABLE", reason="基准点不是候选引用的祖先提交")
        record = reader.record(sha)
        if record["commit"]["parent_count"] > 1:
            raise PredictionError("PRED_INVALID_INPUT", reason="候选引用指向合并提交，暂不支持评审")
        files = reader.files(sha, [base_sha])
        PredictionService._check_files(files)
        commit = dict(record["commit"])
        commit["parents"], commit["parent_count"] = [base_sha], 1
        try:
            return freeze_native_record(
                reader, {"commit": commit, "identity": record["identity"], "files": files}
            )
        except FeatureInputError as error:
            raise PredictionError(
                "PRED_HISTORY_UNAVAILABLE", reason="候选提交与冻结历史不一致：" + error.code
            ) from None

    @staticmethod
    def _candidate_diff(text, base_sha, history, actor_id):
        deltas = _parse_unified_diff(text)
        canonical = text.replace("\r\n", "\n")
        sha = hashlib.sha256(canonical.encode()).hexdigest()
        base = history[base_sha]
        # 时间取基准点之后一微秒，保证候选在祖先排序里严格晚于基准点。
        return CommitInput(
            sha,
            (base_sha,),
            base.event_time + timedelta(microseconds=1),
            hashlib.sha256(("user:" + actor_id).encode()).hexdigest(),
            tuple(deltas),
        )

    def _candidate(self, reader, request, base_sha, history, actor_id):
        if request.get("candidate_ref"):
            return self._candidate_ref(reader, request["candidate_ref"], base_sha)
        return self._candidate_diff(request["candidate_diff"], base_sha, history, actor_id)

    def _candidate_vector(self, history, candidate, repository_id, tick):
        snapshot = Snapshot(
            repository_id, candidate.sha, tuple([*history.values(), candidate]), "full"
        )
        source = _source_digest(snapshot, "candidate")
        index = {commit.sha: commit for commit in history.values()}
        row = vector(
            candidate, ancestry(candidate, index, tick), snapshot, source, "candidate", tick
        )
        return dict(row.values), dict(row.reasons)

    # ---- prediction kernel ----

    def _predict(self, bundle, row, explain):
        import numpy as np

        matrix = np.asarray([row], dtype=float)
        raw = np.clip(bundle["model"].predict_proba(matrix)[:, 1], 1e-6, 1 - 1e-6)
        calibrated = bundle["calibrator"].predict_proba(
            np.log(raw / (1 - raw)).reshape(-1, 1)
        )[:, 1]
        probability = round(float(calibrated[0]), 6)
        if explain:
            method, factors = self._explain(bundle, matrix)
            return probability, method, factors[0]
        return probability, None, None

    @staticmethod
    def _explain(bundle, matrix):
        import numpy as np

        model = bundle["model"]
        if hasattr(model, "named_steps") and "standardscaler" in model.named_steps:
            scaler = model.named_steps["standardscaler"]
            coefficients = model.named_steps["logisticregression"].coef_[0]
            contributions = np.asarray(matrix, dtype=float) * (coefficients / scaler.scale_)
            method = "lr-scaled-coefficient-v1"
        else:
            import shap

            values = shap.TreeExplainer(model).shap_values(np.asarray(matrix, dtype=float))
            values = values[1] if isinstance(values, list) else values
            contributions = np.asarray(values, dtype=float)
            method = "rf-shap-tree-v1"
        factors = []
        for row_index in range(len(matrix)):
            entries = []
            for column_index, column in enumerate(MODEL_COLUMNS):
                contribution = float(contributions[row_index][column_index])
                entries.append(
                    {
                        "feature": column,
                        "value": round(float(matrix[row_index][column_index]), 6),
                        "contribution": round(contribution, 6),
                        "direction": "increase" if contribution >= 0 else "decrease",
                    }
                )
            entries.sort(
                key=lambda item: (abs(item["contribution"]), item["feature"]), reverse=True
            )
            factors.append(entries)
        return method, factors

    def _result(self, bundle, values, reasons, candidate):
        missing = []
        for column in MODEL_COLUMNS:
            product = COLUMN_FEATURES[column]
            if values[product] is None:
                missing.append(f"{product}（{reasons.get(product, 'unknown')}）")
        if missing:
            raise PredictionError(
                "PRED_FEATURE_UNAVAILABLE", reason="模型特征列无法重算：" + "、".join(missing)
            )
        row = [float(values[COLUMN_FEATURES[column]]) for column in MODEL_COLUMNS]
        probability, method, factors = self._predict(bundle, row, explain=True)
        threshold = bundle["threshold"]
        effort = int(values["LA"] + values["LD"])
        return {
            "candidate_sha": candidate.sha,
            "event_time": candidate.event_time,
            "probability": probability,
            "threshold": threshold,
            "risk_level": _level(probability, threshold),
            "effort": effort,
            "risk_density": round(probability / max(1, effort), 6),
            "oversized": effort > OVERSIZED_EFFORT,
            "features": {column: round(row[i], 6) for i, column in enumerate(MODEL_COLUMNS)},
            "reasons": dict(reasons),
            "explanation": {"method": method, "factors": factors},
            "input_json": encode_input(candidate),
        }

    # ---- API side: single candidate with one-second sync attempt ----

    def create_single(self, user, key, request, request_id):
        key_valid(key)
        canonical_diff = (
            request["candidate_diff"].replace("\r\n", "\n")
            if request["candidate_diff"] is not None
            else None
        )
        payload = {
            "prediction_version": VERSION,
            "repository_id": request["repository_id"],
            "model_run_id": request["model_run_id"],
            "base_ref": request["base_ref"],
            "candidate_ref": request["candidate_ref"],
            "candidate_diff": (
                hashlib.sha256(canonical_diff.encode()).hexdigest()
                if canonical_diff is not None
                else None
            ),
        }
        scope = user.id + ":prediction.candidate"
        with Session(self.engine) as db, db.begin():
            self.tasks.actor(db, user)
            if replay := self._existing(db, "prediction.candidate", scope, key, payload):
                return self._replay(db, replay)
            repo, feature_run, frozen = self._feature_context(db, request["repository_id"])
            self.eligible_model(db, request["model_run_id"])
        sync_request = dict(request)
        sync_request["candidate_diff"] = canonical_diff
        try:
            result = self._synchronize(sync_request, repo, feature_run, frozen, payload, user.id)
        except SyncTimeout:
            result = None
        if result is not None:
            return self._commit_single(
                user, key, payload, scope, request, request_id, feature_run, result
            )
        return self._enqueue(user, key, payload, scope, request, request_id)

    def _synchronize(self, request, repo, feature_run, frozen, payload, actor_id):
        started = time.monotonic()

        def budget():
            if time.monotonic() - started > SYNC_SECONDS:
                raise SyncTimeout

        bare = self._bare(repo, feature_run)
        attempt = None
        try:
            attempt = self.storage.attempt(repo.id, str(uuid4()))
            reader = self._reader(bare, attempt, self.settings, self.engine, budget)
            with Session(self.engine) as db:
                run = self.eligible_model(db, payload["model_run_id"])
            bundle = self.load_bundle(run)
            budget()
            base_sha = self._resolve_base(reader, payload["base_ref"])
            reachable = set(reader.plan(base_sha, None))
            history = {}
            for index, data in enumerate(frozen):
                if index % 128 == 0:
                    budget()
                if data["sha"] in reachable:
                    history[data["sha"]] = decode_input(data)
            if base_sha not in history:
                raise PredictionError(
                    "PRED_BASE_UNREACHABLE", reason="基准点在仓库历史中不可达或未导入"
                )
            if reachable - set(history):
                raise PredictionError(
                    "PRED_HISTORY_UNAVAILABLE", reason="基准点的部分历史提交不在特征轮次覆盖范围内"
                )
            budget()
            candidate = self._candidate(reader, request, base_sha, history, actor_id)
            budget()
            values, reasons = self._candidate_vector(history, candidate, repo.id, budget)
            budget()
            result = self._result(bundle, values, reasons, candidate)
            result["base_sha"] = base_sha
            budget()
            return result
        except FeatureInputError as error:
            raise PredictionError(
                "PRED_HISTORY_UNAVAILABLE", reason="历史特征无法重放：" + error.code
            ) from None
        finally:
            if attempt is not None:
                self.storage.remove(attempt)

    def _commit_single(self, user, key, payload, scope, request, request_id, feature_run, result):
        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := self._existing(db, "prediction.candidate", scope, key, payload):
                    return self._replay(db, replay)
                self._feature_context(db, request["repository_id"])
                self.eligible_model(db, request["model_run_id"])
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    payload,
                    scope,
                    request_id,
                    kind="prediction.candidate",
                    enqueue=False,
                )
                point = self._point(db, task, request, feature_run, result)
                row = self._insert_result(db, point, 0, result)
                task.result_json = {"prediction_id": row.id, "results": 1}
                task.progress, task.processed, task.total = 100, 1, 1
                self.tasks.terminal(db, task, "succeeded")
                audit(db, task, "prediction.result")
                audit(db, task, "task.succeeded")
                prediction_id, task_id = row.id, task.id
            with Session(self.engine) as db:
                row = db.get(PredictionResult, prediction_id)
                return self._single_response(row, db.get(PredictionTask, row.task_id))
        except IntegrityError as exc:
            if exc.orig and exc.orig.args and exc.orig.args[0] == 1062:
                with Session(self.engine) as db:
                    if replay := self._existing(db, "prediction.candidate", scope, key, payload):
                        return self._replay(db, replay)
            raise

    def _enqueue(self, user, key, payload, scope, request, request_id):
        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := self._existing(db, "prediction.candidate", scope, key, payload):
                    return self._replay(db, replay)
                _, feature_run, _ = self._feature_context(db, request["repository_id"])
                self.eligible_model(db, request["model_run_id"])
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    payload,
                    scope,
                    request_id,
                    kind="prediction.candidate",
                )
                self._point(db, task, request, feature_run, None)
                return self.tasks.acknowledgement(task) | {"prediction_task_id": task.id}
        except IntegrityError as exc:
            if exc.orig and exc.orig.args and exc.orig.args[0] == 1062:
                with Session(self.engine) as db:
                    if replay := self._existing(db, "prediction.candidate", scope, key, payload):
                        return self._replay(db, replay)
            raise

    # ---- API side: batch ----

    def create_batch(self, user, key, request, request_id):
        key_valid(key)
        window_days, batch_limit = request["window_days"], request["max_candidates"]
        if not 1 <= window_days <= MAX_WINDOW_DAYS:
            raise PredictionError("PRED_INPUT_LIMIT", reason=f"时间窗超出上限 {MAX_WINDOW_DAYS} 天")
        if not 1 <= batch_limit <= MAX_BATCH_CANDIDATES:
            raise PredictionError(
                "PRED_INPUT_LIMIT", reason=f"候选数量超出上限 {MAX_BATCH_CANDIDATES} 条"
            )
        payload = {
            "prediction_version": VERSION,
            "repository_id": request["repository_id"],
            "model_run_id": request["model_run_id"],
            "base_ref": request["base_ref"],
            "window_days": window_days,
            "batch_limit": batch_limit,
        }
        scope = user.id + ":prediction.batch"
        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := self._existing(db, "prediction.batch", scope, key, payload):
                    return self.tasks.acknowledgement(replay) | {
                        "prediction_task_id": replay.root_task_id
                    }
                _, feature_run, _ = self._feature_context(db, request["repository_id"])
                self.eligible_model(db, request["model_run_id"])
                task = self.tasks.new_task(
                    db, user.id, key, payload, scope, request_id, kind="prediction.batch"
                )
                self._point(db, task, request, feature_run, None)
                return self.tasks.acknowledgement(task) | {"prediction_task_id": task.id}
        except IntegrityError as exc:
            if exc.orig and exc.orig.args and exc.orig.args[0] == 1062:
                with Session(self.engine) as db:
                    if replay := self._existing(db, "prediction.batch", scope, key, payload):
                        return self.tasks.acknowledgement(replay) | {
                            "prediction_task_id": replay.root_task_id
                        }
            raise

    @staticmethod
    def _existing(db, kind, scope, key, payload):
        task = db.scalar(
            select(AsyncTask).where(
                AsyncTask.type == kind,
                AsyncTask.scope_key == scope,
                AsyncTask.idempotency_key == key,
            )
        )
        if not task:
            return None
        if task.payload_hash != payload_hash(scope, payload, kind):
            raise PredictionError("TASK_IDEMPOTENCY_CONFLICT", 409)
        return task

    @staticmethod
    def _point(db, task, request, feature_run, result):
        candidate_ref = request.get("candidate_ref")
        candidate_diff = request.get("candidate_diff")
        if result:
            summary = {
                "files": len(result["input_json"]["files"]),
                "lines": sum(
                    (file["insertions"] or 0) + (file["deletions"] or 0)
                    for file in result["input_json"]["files"]
                ),
            }
        elif candidate_diff is not None:
            summary = {"diff": candidate_diff}
        elif request.get("window_days") is not None:
            summary = {
                "window_days": request["window_days"],
                "max_candidates": request["max_candidates"],
            }
        else:
            summary = None
        point = PredictionTask(
            id=task.id,
            repository_id=request["repository_id"],
            model_run_id=request["model_run_id"],
            base_sha=result["base_sha"] if result else "",
            base_ref=request["base_ref"],
            candidate_kind=(
                "batch"
                if request.get("window_days") is not None
                else "single_ref"
                if candidate_ref
                else "single_diff"
            ),
            candidate_ref=candidate_ref,
            candidate_sha=result["candidate_sha"] if result else None,
            candidate_json=summary,
            feature_version=FEATURE_VERSION,
            feature_run_id=feature_run.root_task_id,
            window_days=request.get("window_days"),
            batch_limit=request.get("max_candidates"),
            input_digest=digest(request),
        )
        db.add(point)
        db.flush()
        return point

    @staticmethod
    def _insert_result(db, point, ordinal, result):
        row = PredictionResult(
            task_id=point.id,
            candidate_ordinal=ordinal,
            candidate_sha=result["candidate_sha"],
            event_time=result["event_time"],
            probability=result["probability"],
            threshold=result["threshold"],
            risk_level=result["risk_level"],
            effort=result["effort"],
            risk_density=result["risk_density"],
            oversized=result["oversized"],
            numeric_json=result["features"],
            reasons_json=result["reasons"],
            explanation_json=result["explanation"],
            input_json=result["input_json"],
        )
        db.add(row)
        db.flush()
        return row

    def _replay(self, db, task):
        if task.status == "succeeded":
            point = db.get(PredictionTask, task.root_task_id)
            rows = (
                db.scalars(
                    select(PredictionResult).where(PredictionResult.task_id == point.id)
                ).all()
                if point
                else []
            )
            if len(rows) == 1:
                return self._single_response(rows[0], point)
        return self.tasks.acknowledgement(task) | {"prediction_task_id": task.root_task_id}

    @staticmethod
    def _single_response(row, point):
        return {
            "prediction_id": row.id,
            "model_run_id": point.model_run_id,
            "probability": float(row.probability),
            "calibration": CALIBRATION,
            "risk_level": row.risk_level,
            "effort": row.effort,
            "risk_density": float(row.risk_density),
            "threshold": float(row.threshold),
            "feature_version": point.feature_version,
            "top_factors": row.explanation_json["factors"][:5],
            "limitations": [DISCLAIMER],
        }

    # ---- API side: queue and explanation ----

    def queue(self, task_id, page=1, page_size=20, level=None, oversized=None):
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        with Session(self.engine) as db:
            point = db.get(PredictionTask, task_id)
            if not point:
                raise PredictionError("PRED_NOT_FOUND", 404)
            task = db.get(AsyncTask, point.id)
            conditions = [PredictionResult.task_id == point.id]
            if level:
                conditions.append(PredictionResult.risk_level == level)
            if oversized is not None:
                conditions.append(PredictionResult.oversized.is_(oversized))
            total = db.scalar(select(func.count()).select_from(PredictionResult).where(*conditions))
            rows = db.scalars(
                select(PredictionResult)
                .where(*conditions)
                .order_by(
                    PredictionResult.probability.desc(),
                    PredictionResult.event_time,
                    PredictionResult.candidate_sha,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            threshold = db.scalar(
                select(PredictionResult.threshold)
                .where(PredictionResult.task_id == point.id)
                .limit(1)
            )
            return {
                "prediction_task_id": point.id,
                "task_id": task.id,
                "status": task.status,
                "repository_id": point.repository_id,
                "model_run_id": point.model_run_id,
                "base_ref": point.base_ref,
                "candidate_kind": point.candidate_kind,
                "feature_version": point.feature_version,
                "threshold": float(threshold) if threshold is not None else None,
                "empty": task.status == "succeeded" and total == 0,
                "items": [self._queue_item(row) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    @staticmethod
    def _queue_item(row):
        return {
            "prediction_id": row.id,
            "candidate_sha": row.candidate_sha,
            "event_time": iso(row.event_time),
            "probability": float(row.probability),
            "risk_level": row.risk_level,
            "effort": row.effort,
            "risk_density": float(row.risk_density),
            "oversized": bool(row.oversized),
            "top_factors": row.explanation_json["factors"][:3],
        }

    def explanation(self, prediction_id):
        with Session(self.engine) as db:
            row = db.get(PredictionResult, prediction_id)
            if not row:
                raise PredictionError("PRED_NOT_FOUND", 404)
            point = db.get(PredictionTask, row.task_id)
            return {
                "prediction_id": row.id,
                "candidate_sha": row.candidate_sha,
                "event_time": iso(row.event_time),
                "probability": float(row.probability),
                "risk_level": row.risk_level,
                "model_run_id": point.model_run_id,
                "feature_version": point.feature_version,
                "threshold": float(row.threshold),
                "method": row.explanation_json["method"],
                "factors": row.explanation_json["factors"],
                "limitations": [DISCLAIMER],
            }

    # ---- worker side ----

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type not in {"prediction.candidate", "prediction.batch"}
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
        if task.payload.get("prediction_version") != VERSION:
            self.tasks.terminal(db, task, "failed", "PRED_VERSION_CONFLICT")
            audit(db, task, "prediction.failed")
            return None
        point = db.get(PredictionTask, task.root_task_id, with_for_update=True)
        repo = db.get(Repository, point.repository_id, with_for_update=True) if point else None
        if not point or not repo:
            return None
        if (
            point.repository_id != task.payload["repository_id"]
            or point.model_run_id != task.payload["model_run_id"]
        ):
            raise PredictionError("PRED_STATE_CONFLICT", 409)
        return task, point, repo, now

    def heartbeat(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, _, _, now = context
            task.stage, task.heartbeat_at = "predicting_candidates", now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            return True

    def fail(self, task_id, token, code):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return
            self.tasks.terminal(db, context[0], "failed", code)
            audit(db, context[0], "prediction.failed")

    def source(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            task, point, repo, _ = context
            feature_run = db.get(FeatureRun, point.feature_run_id)
            if (
                not feature_run
                or feature_run.repository_id != repo.id
                or feature_run.purpose != "historical"
                or feature_run.feature_version != FEATURE_VERSION
                or point.feature_version != FEATURE_VERSION
                or feature_run.history_coverage != "full"
                or feature_run.quality_json is None
            ):
                raise PredictionError(
                    "PRED_HISTORY_UNAVAILABLE", reason="历史特征轮次不可用或口径不一致"
                )
            rows = db.scalars(
                select(FeatureItem)
                .where(FeatureItem.root_task_id == feature_run.root_task_id)
                .order_by(FeatureItem.ordinal)
            ).all()
            frozen = [row.input_json for row in rows]
            if digest(frozen) != feature_run.input_digest:
                raise PredictionError(
                    "PRED_HISTORY_UNAVAILABLE", reason="历史特征轮次的冻结输入摘要不一致"
                )
            bare = self._bare(repo, feature_run)
            return task, point, repo, bare, frozen

    def plan(self, task_id, token, reader, payload):
        try:
            return self._plan(task_id, token, reader, payload)
        except FeatureInputError as error:
            raise RepositoryError(409, error.code) from None

    def _plan(self, task_id, token, reader, payload):
        original_tick = reader.tick

        def tick():
            if time.monotonic() > reader.deadline:
                raise RepositoryError(408, "REPOSITORY_PARSE_TIMEOUT")
            original_tick()

        reader.tick = tick
        source = self.source(task_id, token)
        if source is None:
            raise ExecutionStopped
        task, point, repo, _, frozen = source
        with Session(self.engine) as db:
            run = self.eligible_model(db, point.model_run_id)
        bundle = self.load_bundle(run)
        base_sha = self._resolve_base(reader, payload["base_ref"])
        reachable = set(reader.plan(base_sha, None))
        history = {}
        for index, data in enumerate(frozen):
            if index % 128 == 0:
                tick()
            if data["sha"] in reachable:
                history[data["sha"]] = decode_input(data)
        if base_sha not in history:
            raise PredictionError(
                "PRED_BASE_UNREACHABLE", reason="基准点在仓库历史中不可达或未导入"
            )
        if reachable - set(history):
            raise PredictionError(
                "PRED_HISTORY_UNAVAILABLE", reason="基准点的部分历史提交不在特征轮次覆盖范围内"
            )
        self._base_sha, self._base_time = base_sha, history[base_sha].event_time
        self._candidate_sha = None
        if payload.get("candidate_ref") or payload.get("candidate_diff") is not None:
            if payload["candidate_diff"] is not None:
                stored = point.candidate_json.get("diff") if point.candidate_json else None
                if (
                    not stored
                    or hashlib.sha256(stored.replace("\r\n", "\n").encode()).hexdigest()
                    != payload["candidate_diff"]
                ):
                    raise PredictionError(
                        "PRED_INVALID_INPUT", reason="候选改动内容与请求摘要不一致"
                    )
            request = {
                "candidate_ref": payload["candidate_ref"],
                "candidate_diff": point.candidate_json.get("diff") if point.candidate_json else None,
            }
            candidate = self._candidate(reader, request, base_sha, history, task.actor_id)
            self._candidate_sha = candidate.sha
            values, reasons = self._candidate_vector(history, candidate, repo.id, tick)
            result = self._result(bundle, values, reasons, candidate)
            self.prepared_results = {candidate.sha: result}
            return [candidate.sha]
        window = timedelta(days=payload["window_days"])
        candidates = sorted(
            (
                commit
                for commit in history.values()
                if commit.sha != base_sha
                and self._base_time - window <= commit.event_time <= self._base_time
            ),
            key=lambda commit: commit.order,
        )[-payload["batch_limit"] :]
        snapshot = Snapshot(repo.id, base_sha, tuple(history.values()), "full")
        rows = {row.sha: row for row in compute(snapshot, "candidate", tick)}
        self.prepared_results = {}
        for candidate in candidates:
            tick()
            row = rows[candidate.sha]
            self.prepared_results[candidate.sha] = self._result(
                bundle, dict(row.values), dict(row.reasons), candidate
            )
        return [candidate.sha for candidate in candidates]

    def prepare(self, task_id, token, plan_hash, total):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            task, point, _, now = context
            published = db.scalar(
                select(func.count())
                .select_from(PredictionResult)
                .where(PredictionResult.task_id == point.id)
            )
            if point.plan_hash is None:
                point.plan_hash, point.total = plan_hash, total
            elif point.plan_hash != plan_hash or point.total != total:
                raise PredictionError("PRED_STATE_CONFLICT", 409)
            if not point.base_sha:
                point.base_sha = self._base_sha
                point.base_time = self._base_time
            if point.candidate_sha is None and self._candidate_sha:
                point.candidate_sha = self._candidate_sha
            task.total, task.stage = total, "predicting_candidates"
            task.heartbeat_at = now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            return published

    def record(self, task_id, token, reader, sha):
        return self.prepared_results[sha]

    def batch(self, task_id, token, expected, records, plan_hash, complete=False):
        self.storage.capacity()
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, point, _, now = context
            published = db.scalar(
                select(func.count())
                .select_from(PredictionResult)
                .where(PredictionResult.task_id == point.id)
            )
            if published != expected or point.plan_hash != plan_hash:
                return False
            end = expected + len(records)
            if point.total is None or end > point.total or (complete and end != point.total):
                raise PredictionError("PRED_STATE_CONFLICT", 409)
            for ordinal, record in enumerate(records, expected):
                self._insert_result(db, point, ordinal, record)
            task.processed, task.total = end, point.total
            task.progress = 100 if complete else min(99, 100 * end / max(point.total, 1))
            task.stage, task.heartbeat_at = "predicting_candidates", now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            audit(db, task, "prediction.result")
            if complete:
                task.result_json = {
                    "prediction_task_id": point.id,
                    "repository_id": point.repository_id,
                    "model_run_id": point.model_run_id,
                    "threshold": records[-1]["threshold"] if records else None,
                    "results": end,
                    "sort": {"probability": "desc", "event_time": "asc", "candidate_sha": "asc"},
                }
                self.tasks.terminal(db, task, "succeeded")
                audit(db, task, "task.succeeded")
            return True


class PredictionExecutor:
    def __init__(self, settings, service):
        self.settings, self.service, self.storage = settings, service, service.storage

    def run(self, task_id, token, payload):
        attempt = None
        try:
            self.storage.capacity()
            source = self.service.source(task_id, token)
            if source is None:
                return False
            bare = source[3]
            attempt = self.storage.attempt(payload["repository_id"], str(uuid4()))
            clone = CloneExecutor(
                self.settings,
                RepositoryService(self.settings, SimpleNamespace(engine=self.service.engine)),
            )
            if not clone.git:
                raise RepositoryError(503, "REPOSITORY_GIT_UNAVAILABLE")
            environment, _ = clone.environment(attempt, SimpleNamespace(port=0))
            environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
            configs = {
                "protocol.https.allow": "never",
                "diff.external": "",
                "core.attributesFile": os.devnull,
            }
            start = int(environment["GIT_CONFIG_COUNT"])
            for index, (key, value) in enumerate(configs.items(), start):
                environment[f"GIT_CONFIG_KEY_{index}"] = key
                environment[f"GIT_CONFIG_VALUE_{index}"] = value
            environment["GIT_CONFIG_COUNT"] = str(start + len(configs))
            heartbeat = 0

            def tick():
                nonlocal heartbeat
                self.storage.capacity()
                if time.monotonic() - heartbeat >= min(1, self.settings.task_heartbeat_seconds):
                    if not self.service.heartbeat(task_id, token):
                        raise ExecutionStopped
                    heartbeat = time.monotonic()

            reader = NativeReader(
                bare,
                attempt,
                environment,
                tick,
                time.monotonic() + self.settings.repository_parse_timeout_seconds,
                clone.git,
            )
            plan = self.service.plan(task_id, token, reader, payload)
            plan_hash = hashlib.sha256(
                json.dumps(plan, separators=(",", ":")).encode()
            ).hexdigest()
            total = len(plan)
            resume = self.service.prepare(task_id, token, plan_hash, total)
            if resume is None:
                return False
            processed = resume
            while processed < total:
                records = []
                for sha in plan[processed : processed + BATCH_SIZE]:
                    tick()
                    records.append(self.service.record(task_id, token, reader, sha))
                if not self.service.batch(
                    task_id,
                    token,
                    processed,
                    records,
                    plan_hash,
                    complete=processed + len(records) == total,
                ):
                    return False
                processed += len(records)
            if not total or processed == resume:
                return self.service.batch(task_id, token, processed, [], plan_hash, complete=True)
            return True
        except ExecutionStopped:
            # Finalize a requested cancellation outside the interrupted record transaction.
            self.service.heartbeat(task_id, token)
            return False
        except (RepositoryError, PredictionError) as error:
            self.service.fail(task_id, token, error.code)
            return False
        finally:
            if attempt is not None:
                self.storage.remove(attempt)
