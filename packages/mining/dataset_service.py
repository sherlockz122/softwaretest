"""Application research datasets: fenced batches, recoverable roots, atomic readiness."""

import csv
import os
import shutil
import time
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.source_evidence import content_hash
from packages.persistence.models import (
    AsyncTask,
    DatasetMember,
    DatasetObservation,
    DatasetVersion,
    Repository,
)
from packages.tasks.service import TaskError, TaskService, audit, database_now, key_valid

BATCH_SIZE = 500
MAX_BUILD_SECONDS = 900


class DatasetStopped(Exception):
    pass


class DatasetService:
    def __init__(self, settings, connections, *, store=None, output=None):
        self.settings, self.engine = settings, connections.engine
        self.tasks = TaskService(settings, connections)
        self.store, self.output = store or research.STORE, output or research.OUTPUT

    def create(self, user, key, request, request_id):
        key_valid(key)
        policy = {
            k: request[k]
            for k in ("purpose", "split_strategy", "train_ratio", "validation_ratio", "test_ratio")
        }
        research.policy_valid(policy)
        scope = user.id + ":dataset.build"
        if request.get("source_kind", "asset") not in ("asset", "repository"):
            raise research.ResearchError("DATA_POLICY_INVALID")
        if request.get("source_kind") == "repository":
            binding = self.observation_binding(request)
        else:
            binding = None

        def existing(db):
            row = db.scalar(
                select(AsyncTask).where(
                    AsyncTask.type == "dataset.build",
                    AsyncTask.scope_key == scope,
                    AsyncTask.idempotency_key == key,
                )
            )
            if row and row.payload["request"] != request:
                raise TaskError(409, "TASK_IDEMPOTENCY_CONFLICT")
            return (
                (self.tasks.acknowledgement(row) | {"dataset_id": row.root_task_id})
                if row
                else None
            )

        try:
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := existing(db):
                    return replay
                if binding is not None:
                    # 同一仓库、同一轮次组合与同一政策的重复请求返回同一数据集版本。
                    # 政策列是 MySQL 原生 JSON，等值比较在 SQL 层受格式影响，先按绑定
                    # 缩小范围再在 Python 侧比较政策字典。
                    rows = db.execute(
                        select(DatasetObservation.dataset_version_id, DatasetVersion.policy_json)
                        .join(
                            DatasetVersion,
                            DatasetVersion.id == DatasetObservation.dataset_version_id,
                        )
                        .where(
                            DatasetObservation.binding_hash == binding,
                            DatasetVersion.output_json.is_not(None),
                        )
                    ).all()
                    for repeated, stored_policy in rows:
                        if stored_policy == policy:
                            task = db.get(AsyncTask, repeated)
                            if task:
                                return self.tasks.acknowledgement(task) | {
                                    "dataset_id": repeated
                                }
                if binding is not None:
                    # 只做有界预检；轮次摘要与成熟度由 Worker 在构建时全量校验
                    if not db.get(Repository, request["repository_id"]):
                        raise research.ResearchError("DATA_SOURCE_INVALID")
                    asset_id = source_hash = binding
                else:
                    # Only bounded metadata; heavyweight verification is performed by the Worker.
                    source = research.preview(request["asset_id"], self.store)
                    if source["source_hash"] != request["source_hash"]:
                        raise research.ResearchError("DATA_SOURCE_DRIFT")
                    asset_id, source_hash = request["asset_id"], request["source_hash"]
                task = self.tasks.new_task(
                    db,
                    user.id,
                    key,
                    {
                        "dataset_version": research.VERSION,
                        "dataset_rule_hash": research.RULE_HASH,
                        "request": request,
                    },
                    scope,
                    request_id,
                    kind="dataset.build",
                )
                db.add(
                    DatasetVersion(
                        id=task.id,
                        latest_task_id=task.id,
                        name=request["name"],
                        purpose=request["purpose"],
                        asset_id=asset_id,
                        source_hash=source_hash,
                        policy_json=policy,
                    )
                )
                if binding is not None:
                    db.flush()
                    db.add(
                        DatasetObservation(
                            id=str(uuid4()),
                            dataset_version_id=task.id,
                            repository_id=request["repository_id"],
                            feature_run_id=request["feature_run_id"],
                            szz_run_ids_json=request["szz_run_ids"],
                            binding_hash=binding,
                        )
                    )
                db.flush()
                return self.tasks.acknowledgement(task) | {"dataset_id": task.id}
        except IntegrityError as exc:
            if exc.orig.args[0] != 1062:
                raise
            with Session(self.engine) as db, db.begin():
                self.tasks.actor(db, user)
                if replay := existing(db):
                    return replay
            raise

    FEATURE_TO_RESEARCH = {
        "NS": "ns",
        "ND": "nd",
        "NF": "nf",
        "Entropy": "ent",
        "LA": "la",
        "LD": "ld",
        "NDEV": "ndev",
        "AGE": "age",
        "NUC": "nuc",
        "EXP": "aexp",
        "REXP": "arexp",
        "SEXP": "asexp",
    }
    RESEARCH_TO_FEATURE = {name: feature for feature, name in FEATURE_TO_RESEARCH.items()}

    @staticmethod
    def observation_binding(request):
        """把仓库与轮次组合归一化成绑定哈希；轮次内容摘要由构建时全量校验。"""
        szz_run_ids = request.get("szz_run_ids")
        if (
            not isinstance(szz_run_ids, list)
            or not 1 <= len(szz_run_ids) <= 32
            or len(set(szz_run_ids)) != len(szz_run_ids)
            or any(type(value) is not str or not value.strip() for value in szz_run_ids)
        ):
            raise research.ResearchError("DATA_OBSERVATION_SELECTION_INVALID")
        for field in ("repository_id", "feature_run_id"):
            if type(request.get(field)) is not str or not request[field].strip():
                raise research.ResearchError("DATA_OBSERVATION_SELECTION_INVALID")
            try:
                UUID(request[field])
            except (ValueError, AttributeError):
                raise research.ResearchError("DATA_OBSERVATION_SELECTION_INVALID") from None
        for value in szz_run_ids:
            try:
                UUID(value)
            except (ValueError, AttributeError):
                raise research.ResearchError("DATA_OBSERVATION_SELECTION_INVALID") from None
        return content_hash(
            {
                "repository_id": request["repository_id"],
                "feature_run_id": request["feature_run_id"],
                "szz_run_ids": sorted(szz_run_ids),
            }
        )

    def prepare_repository(self, request, policy, check):
        """仓库轮次 → 成熟两类样本行与元数据；审计不足时明确拒绝并写明原因。"""
        from types import SimpleNamespace

        from packages.mining.dataset import DatasetError
        from packages.mining.dataset import audit as maturity_audit
        from packages.mining.dataset_source import DatasetSource

        source = DatasetSource(self.settings, SimpleNamespace(engine=self.engine))
        ratios = {k: policy[k + "_ratio"] for k in research.PARTITIONS}
        snapshot, vectors, observations, audit_policy = source.load(
            request["repository_id"],
            request["feature_run_id"],
            request["szz_run_ids"],
            ratios=ratios,
        )
        result = maturity_audit(snapshot, vectors, observations, audit_policy, tick=check)
        if not result["ready"]:
            raise DatasetError("DATA_MATURE_SAMPLES_INSUFFICIENT")
        with Session(self.engine) as db:
            repository = db.get(Repository, request["repository_id"])
            if repository is None:
                raise research.ResearchError("DATA_SOURCE_INVALID")
            url = (repository.canonical_url or "").rstrip("/")
            repository_name = url.split("://", 1)[-1].removesuffix(".git") or url
        members, excluded_missing = self.members_from_audit(result, repository_name, check)
        counts = {
            partition: {
                "clean": sum(
                    1 for m in members if m["partition"] == partition and m["label"] == 0
                ),
                "buggy": sum(
                    1 for m in members if m["partition"] == partition and m["label"] == 1
                ),
            }
            for partition in research.PARTITIONS
            if any(m["partition"] == partition for m in members)
        }
        if (
            not members
            or excluded_missing
            and not any(m["label"] for m in members)
            or any(not value["clean"] or not value["buggy"] for value in counts.values())
        ):
            raise DatasetError("DATA_MATURE_SAMPLES_INSUFFICIENT")
        binding = self.observation_binding(request)
        metadata = {
            "version": research.VERSION,
            "rule_hash": result["rule_hash"],
            "numeric_encoding": "canonical-json-text-roundtrip-v1",
            "policy": policy,
            "source_hash": binding,
            "asset_id": binding,
            "asset_quality_hash": result["source_digest"],
            "source_acquisition": {
                "source_kind": "repository_observation",
                "repository_id": request["repository_id"],
                "feature_run_id": request["feature_run_id"],
                "szz_run_ids": sorted(request["szz_run_ids"]),
                "binding_hash": binding,
                "snapshot_at": result["policy"]["snapshot_at"],
                "label_version": result["label_version"],
                "feature_version": result["feature_version"],
                "head_sha": result["head_sha"],
            },
            "source_first_stored_at": None,
            "rows": len(members),
            "original_rows": len(result["labels"]),
            "duplicate_rows": 0,
            "excluded_missing_rows": excluded_missing,
            "unknown_rows": result["label_counts"].get("unknown", 0),
            "members_hash": content_hash(members),
            "partitions": counts,
            "repositories": {
                repository_name: {
                    "clean": sum(1 for m in members if m["label"] == 0),
                    "buggy": sum(1 for m in members if m["label"] == 1),
                }
            },
            "cohorts": {
                repository_name: {
                    "clean": sum(1 for m in members if m["label"] == 0),
                    "buggy": sum(1 for m in members if m["label"] == 1),
                }
            },
            "features": list(research.NUMERIC_FIELDS),
            "auxiliary_features": ["fix", "lt"],
            "lt_source": "kamei14_LT_or_missing",
            "target": "mature_SZZ_binary_time_labels_v1",
            "event_clock": "published_author_epoch_UTC",
            "capabilities": {
                "research_binary": True,
                "historical_replay": False,
                "candidate_release": False,
                "research_semantic": False,
                "type_prediction": False,
                "localization": False,
            },
            "limitations": [
                "mature_SZZ_labels_not_gold",
                "not_historical_online_simulation",
                "unknown_labels_not_included",
                "individual_missing_feature_rows_excluded",
                "no_preprocessing_fitted_or_models_trained",
            ],
        }
        return members, metadata, content_hash(metadata)

    @classmethod
    def members_from_audit(cls, result, repository_name, check=lambda: None):
        """把审计结论转成 research 同构的成员行；缺失特征行排除，绝不插补。"""
        by_sha = {row["sha"]: row for row in result["labels"]}
        members = []
        excluded_missing = 0
        for partition in research.PARTITIONS:
            for sha in result["split"][partition]:
                check()
                row = by_sha[sha]
                # 按 research.NUMERIC_FIELDS 顺序构造：CSV 表头必须与 CSV_FIELDS 一致；
                # 数值统一转 float（内核可能产出整数），CSV 往返后 canonical 才一致
                values = {
                    name: (
                        float(row["values"][cls.RESEARCH_TO_FEATURE[name]])
                        if row["values"][cls.RESEARCH_TO_FEATURE[name]] is not None
                        else None
                    )
                    for name in research.NUMERIC_FIELDS
                }
                if any(value is None for value in values.values()):
                    excluded_missing += 1
                    continue
                fix = row["values"].get("FIX")
                lt = row["values"].get("LT")
                members.append(
                    {
                        "ordinal": len(members),
                        "canonical_repository": repository_name,
                        "commit_id": sha,
                        "event_epoch": int(row["event_time"].timestamp()),
                        "label": 1 if row["label"] == "buggy" else 0,
                        "partition": partition,
                        "numeric12": values,
                        "source_rows": [sha],
                        "cohorts": [repository_name],
                        "fix": fix,
                        "lt": lt,
                        "lt_missing_reason": (
                            row["missing_reasons"].get("LT") if lt is None else None
                        ),
                        **(
                            {"fix_missing_reason": row["missing_reasons"].get("FIX")}
                            if fix is None
                            else {}
                        ),
                    }
                )
        return members, excluded_missing

    def fenced(self, db, task_id, token):
        task = self.tasks.locked(db, task_id)
        now = database_now(db)
        if (
            task.type != "dataset.build"
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
        dataset = db.get(DatasetVersion, task.root_task_id, with_for_update=True)
        if not dataset or dataset.latest_task_id != task.id or dataset.output_json is not None:
            return None
        if (
            task.payload.get("dataset_version") != research.VERSION
            or task.payload.get("dataset_rule_hash") != research.RULE_HASH
        ):
            self.tasks.terminal(db, task, "failed", "DATA_VERSION_CONFLICT")
            audit(db, task, "dataset.failed")
            return None
        return task, dataset, now

    def heartbeat(self, task_id, token):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, dataset, now = context
            task.processed, task.total = dataset.processed, dataset.total
            task.progress = min(99, 100 * dataset.processed / max(dataset.total or 0, 1))
            task.heartbeat_at = now
            task.lease_until = now + timedelta(seconds=self.settings.task_lease_seconds)
            task.version += 1
            return True

    def freeze(self, task_id, token, metadata, plan_hash):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return None
            _, dataset, _ = context
            if (
                dataset.asset_id != metadata["asset_id"]
                or dataset.source_hash != metadata["source_hash"]
                or dataset.policy_json != metadata["policy"]
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            if dataset.plan_hash and (
                dataset.plan_hash != plan_hash or dataset.plan_json != metadata
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            dataset.plan_hash, dataset.plan_json, dataset.total = (
                plan_hash,
                metadata,
                metadata["rows"],
            )
            db.flush()
            return dataset.id, dataset.processed

    def batch(self, task_id, token, expected, members, plan_hash):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, dataset, now = context
            if (
                dataset.plan_hash != plan_hash
                or dataset.processed != expected
                or expected + len(members) > dataset.total
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            db.execute(
                insert(DatasetMember),
                [
                    {
                        "dataset_id": dataset.id,
                        "ordinal": row["ordinal"],
                        "canonical_repository": row["canonical_repository"],
                        "commit_id": row["commit_id"],
                        "event_epoch": row["event_epoch"],
                        "label": row["label"],
                        "partition": row["partition"],
                        "numeric_json": row["numeric12"],
                        "source_json": {
                            "row_numbers": row["source_rows"],
                            "cohorts": row["cohorts"],
                            "fix": row["fix"],
                            "lt": row["lt"],
                            "lt_missing_reason": row["lt_missing_reason"],
                            **(
                                {"fix_missing_reason": row["fix_missing_reason"]}
                                if "fix_missing_reason" in row
                                else {}
                            ),
                        },
                    }
                    for row in members
                ],
            )
            dataset.processed += len(members)
            task.processed, task.total = dataset.processed, dataset.total
            task.progress, task.stage = (
                min(99, 100 * dataset.processed / max(dataset.total, 1)),
                "building_dataset",
            )
            task.heartbeat_at, task.lease_until = (
                now,
                now + timedelta(seconds=self.settings.task_lease_seconds),
            )
            task.version += 1
            db.flush()
            return True

    def artifacts(self, dataset_id, token, members, metadata, check):
        directory = assets.safe_path(self.output / str(UUID(dataset_id)) / str(UUID(token)))
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        budget = assets.Budget(tick=check)
        records = {}
        for partition in research.PARTITIONS:
            if not any(member["partition"] == partition for member in members):
                # 验证集可选：validation_ratio=0 时不产出验证分区文件
                continue
            path = assets.safe_path(directory / (partition + ".csv"))
            with path.open("x", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=research.CSV_FIELDS)
                writer.writeheader()
                for member in members:
                    budget.check()
                    if member["partition"] == partition:
                        writer.writerow(
                            {k: member[k] for k in research.CSV_FIELDS[:4]}
                            | member["numeric12"]
                            | {"fix": member["fix"], "lt": member["lt"]}
                        )
                stream.flush()
                os.fsync(stream.fileno())
            records[partition] = assets.file_digest(path, budget.check)
        output = assets.sealed(
            {"dataset_id": dataset_id, "attempt_id": token, "metadata": metadata, "files": records}
        )
        assets.write_json(directory / "manifest.json", output)
        budget.check()
        return output

    def verify_members(self, dataset_id, members, count, check):
        for offset in range(0, count, BATCH_SIZE):
            check()
            with Session(self.engine) as db:
                rows = db.scalars(
                    select(DatasetMember)
                    .where(
                        DatasetMember.dataset_id == dataset_id,
                        DatasetMember.ordinal >= offset,
                        DatasetMember.ordinal < min(offset + BATCH_SIZE, count),
                    )
                    .order_by(DatasetMember.ordinal)
                ).all()
                expected = members[offset : min(offset + BATCH_SIZE, count)]
                if len(rows) != len(expected):
                    raise research.ResearchError("DATA_MEMBER_DRIFT")
                for row, item in zip(rows, expected, strict=True):
                    if (
                        row.ordinal != item["ordinal"]
                        or row.canonical_repository != item["canonical_repository"]
                        or row.commit_id != item["commit_id"]
                        or row.partition != item["partition"]
                        or row.label != item["label"]
                        or row.event_epoch != item["event_epoch"]
                        or assets.canonical(row.numeric_json) != assets.canonical(item["numeric12"])
                        or row.source_json
                        != {
                            "row_numbers": item["source_rows"],
                            "cohorts": item["cohorts"],
                            "fix": item["fix"],
                            "lt": item["lt"],
                            "lt_missing_reason": item["lt_missing_reason"],
                            **(
                                {"fix_missing_reason": item["fix_missing_reason"]}
                                if "fix_missing_reason" in item
                                else {}
                            ),
                        }
                    ):
                        raise research.ResearchError("DATA_MEMBER_DRIFT")

    def publish(self, task_id, token, plan_hash, output):
        with Session(self.engine) as db, db.begin():
            context = self.fenced(db, task_id, token)
            if not context:
                return False
            task, dataset, _ = context
            if (
                dataset.plan_hash != plan_hash
                or dataset.processed != dataset.total
                or output["metadata"] != dataset.plan_json
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            if (
                db.scalar(
                    select(func.count())
                    .select_from(DatasetMember)
                    .where(DatasetMember.dataset_id == dataset.id)
                )
                != dataset.total
            ):
                raise research.ResearchError("DATA_PLAN_CONFLICT")
            dataset.output_json = output
            task.result_json = {
                "dataset_id": dataset.id,
                "purpose": dataset.purpose,
                "rows": dataset.total,
                "members_hash": dataset.plan_json["members_hash"],
                "manifest_hash": output["content_hash"],
            }
            task.progress = 100
            self.tasks.terminal(db, task, "succeeded")
            audit(db, task, "dataset.publish")
            db.flush()
            return True

    def run(self, task_id, token, payload, *, after_batch=None):
        last = 0
        last_capacity = 0
        deadline = time.monotonic() + MAX_BUILD_SECONDS

        def check():
            nonlocal last, last_capacity
            if time.monotonic() > deadline:
                raise research.ResearchError("DATA_BUILD_DEADLINE")
            if time.monotonic() - last_capacity >= 2:
                destination = assets.safe_path(self.output)
                physical = destination if destination.exists() else destination.parent
                if shutil.disk_usage(physical).free < 2.5 * 1024**3:
                    raise research.ResearchError("DATA_CAPACITY_UNSAFE")
                last_capacity = time.monotonic()
            if time.monotonic() - last >= min(3, self.settings.task_heartbeat_seconds):
                if not self.heartbeat(task_id, token):
                    raise DatasetStopped
                last = time.monotonic()

        try:
            check()
            request = payload["request"]
            policy = {
                k: request[k]
                for k in (
                    "purpose",
                    "split_strategy",
                    "train_ratio",
                    "validation_ratio",
                    "test_ratio",
                )
            }
            if request.get("source_kind") == "repository":
                members, metadata, plan_hash = self.prepare_repository(request, policy, check)
            else:
                members, metadata, plan_hash = research.prepare(
                    research.asset_path(request["asset_id"], self.store),
                    request["source_hash"],
                    policy,
                    tick=check,
                )
            frozen = self.freeze(task_id, token, metadata, plan_hash)
            if frozen is None:
                raise DatasetStopped
            dataset_id, processed = frozen
            self.verify_members(dataset_id, members, processed, check)
            for offset in range(processed, len(members), BATCH_SIZE):
                check()
                if not self.batch(
                    task_id, token, offset, members[offset : offset + BATCH_SIZE], plan_hash
                ):
                    raise DatasetStopped
                if after_batch:
                    after_batch(offset + len(members[offset : offset + BATCH_SIZE]))
            self.verify_members(dataset_id, members, len(members), check)
            output = self.artifacts(dataset_id, token, members, metadata, check)
            self.publish(task_id, token, plan_hash, output)
        except DatasetStopped:
            return
        except (
            research.ResearchError,
            assets.AssetError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            from packages.mining.dataset import DatasetError

            with Session(self.engine) as db, db.begin():
                context = self.fenced(db, task_id, token)
                if context:
                    task, _, _ = context
                    if isinstance(exc, DatasetError):
                        code = exc.code
                        self.tasks.terminal(db, task, "failed", code)
                        task.error_message = (
                            "真实数据不足：仓库轮次审计未产出成熟两类样本，"
                            "数量与原因见任务记录（原始标签不足或观察覆盖不完整），"
                            "不移动切分边界、不伪造样本。"
                        )
                    else:
                        code = (
                            exc.code
                            if isinstance(exc, research.ResearchError)
                            else "DATA_SOURCE_OR_STORAGE_INVALID"
                        )
                        self.tasks.terminal(db, task, "failed", code)
                    audit(db, task, "dataset.failed")

    @staticmethod
    def visible(dataset, task):
        ready = dataset.output_json is not None and task.status == "succeeded"
        return {
            "id": dataset.id,
            "name": dataset.name,
            "purpose": dataset.purpose,
            "asset_id": dataset.asset_id,
            "source_hash": dataset.source_hash,
            "policy": dataset.policy_json,
            "created_at": dataset.created_at.isoformat() + "Z",
            "task_id": task.id,
            "status": task.status,
            "processed": dataset.processed,
            "total": dataset.total,
            "ready": ready,
            "summary": dataset.output_json["metadata"] if ready else None,
            "manifest_hash": dataset.output_json["content_hash"] if ready else None,
        }

    def listing(self, page=1, page_size=20):
        with Session(self.engine) as db:
            total = db.scalar(select(func.count()).select_from(DatasetVersion))
            rows = db.execute(
                select(DatasetVersion, AsyncTask)
                .join(AsyncTask, DatasetVersion.latest_task_id == AsyncTask.id)
                .order_by(DatasetVersion.created_at.desc(), DatasetVersion.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [self.visible(d, t) for d, t in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "assets": research.previews(self.store),
            }

    def detail(self, dataset_id, offset=0, limit=100, filter_partition=None, filter_label=None):
        with Session(self.engine) as db:
            dataset = db.get(DatasetVersion, dataset_id)
            if not dataset:
                raise TaskError(404, "DATA_NOT_FOUND")
            task = db.get(AsyncTask, dataset.latest_task_id)
            result = self.visible(dataset, task)
            result["members"] = []
            result["member_total"] = None
            if result["ready"]:
                conditions = [DatasetMember.dataset_id == dataset_id]
                if filter_partition is not None:
                    conditions.append(DatasetMember.partition == filter_partition)
                if filter_label is not None:
                    conditions.append(DatasetMember.label == filter_label)
                rows = db.scalars(
                    select(DatasetMember)
                    .where(*conditions)
                    .order_by(DatasetMember.ordinal)
                    .offset(offset)
                    .limit(limit)
                )
                result["members"] = [
                    {
                        "ordinal": r.ordinal,
                        "canonical_repository": r.canonical_repository,
                        "commit_id": r.commit_id,
                        "partition": r.partition,
                        "label": r.label,
                        "event_epoch": r.event_epoch,
                        "numeric12": r.numeric_json,
                        "fix": r.source_json.get("fix"),
                        "lt": r.source_json.get("lt"),
                        "lt_missing_reason": r.source_json.get("lt_missing_reason"),
                        "fix_missing_reason": r.source_json.get("fix_missing_reason"),
                        "source": r.source_json,
                    }
                    for r in rows
                ]
                result["member_total"] = db.scalar(
                    select(func.count()).select_from(DatasetMember).where(*conditions)
                )
                result["exports"] = dataset.output_json["files"]
            result.update(offset=offset, limit=limit)
            return result

    def download(self, dataset_id, partition):
        if partition not in research.PARTITIONS:
            raise TaskError(422, "DATA_POLICY_INVALID")
        with Session(self.engine) as db:
            dataset = db.get(DatasetVersion, dataset_id)
            if not dataset:
                raise TaskError(404, "DATA_NOT_FOUND")
            task = db.get(AsyncTask, dataset.latest_task_id)
            if not dataset.output_json or task.status != "succeeded":
                raise TaskError(409, "DATA_NOT_READY")
            output = dataset.output_json
        assets.verify_seal(output)
        if partition not in output["files"]:
            # 验证集可选：没切验证分区的数据集没有 validation.csv
            raise TaskError(404, "DATA_PARTITION_ABSENT")
        directory = assets.safe_path(
            self.output / str(UUID(dataset_id)) / str(UUID(output["attempt_id"]))
        )
        budget = assets.Budget(seconds=30)
        stored = assets.read_json(directory / "manifest.json", budget.check)
        if stored != output:
            raise research.ResearchError("DATA_ARTIFACT_DRIFT")
        path = assets.safe_path(directory / (partition + ".csv"))
        if assets.file_digest(path, budget.check) != output["files"][partition]:
            raise research.ResearchError("DATA_ARTIFACT_DRIFT")
        return path
