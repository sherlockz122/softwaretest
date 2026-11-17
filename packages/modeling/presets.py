"""Versioned preset baselines trained once on the built-in public asset.

预置基线（LR/RF 默认配置）在首次需要时用内置公开资产训练一次并注册进池，
之后同一代码版本内直接复用。预置条目按「契约 + 代码版本」幂等：重复调用不重训，
版本升级（policy.VERSION 变化）后旧条目保留但不再命中自动匹配（匹配按当前
请求契约进行，预置注册函数会为新版本注册新条目）。
"""

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.public_dataset import NUMERIC_FIELDS
from packages.modeling import pool, train
from packages.modeling.policy import DEFAULT_FORMS, PLUGINS, VERSION, ModelError, provenance
from packages.persistence.models import ModelPoolEntry

PRESET_SEED = 42
PRESET_RATIOS = {
    "purpose": research.PURPOSE,
    "split_strategy": "time_ordered",
    "train_ratio": 0.70,
    "validation_ratio": 0.15,
    "test_ratio": 0.15,
}


def preset_specs():
    """当前版本要注册的预置基线清单：默认超参 + 全部十二维特征 + 默认形式。"""
    specs = []
    for plugin in ("lr", "rf"):
        forms = {name: DEFAULT_FORMS[plugin] for name in NUMERIC_FIELDS}
        specs.append(
            {
                "name": f"preset-{plugin}-{VERSION}",
                "plugin": plugin,
                "params": dict(PLUGINS[plugin]["params"]),
                "features": list(NUMERIC_FIELDS),
                "forms": forms,
                "seed": PRESET_SEED,
            }
        )
    return specs


def builtin_asset(store, *, tick=None):
    """在内置资产目录里找第一份内置 apachejit 资产（排除用户导入资产）。"""
    budget = assets.Budget(tick=tick, seconds=60)
    for item in sorted(assets.safe_path(store).iterdir()):
        budget.check()
        if not item.is_dir():
            continue
        try:
            loaded = assets.load_asset(item, tick=budget.check)
        except assets.AssetError:
            continue
        acquisition = loaded["manifest"]["identity"]["source_acquisition"]
        if (
            acquisition.get("source_kind", "builtin_asset") == "builtin_asset"
            and acquisition.get("apachejit_license") == "CC-BY-4.0"
        ):
            return item, loaded
    raise ModelError("MODEL_POOL_PRESET_ASSET_MISSING", 409)


def preset_partitions(members):
    """把 prepare 产出的成员行组装成 train.fit_model 需要的分区字典。"""
    partitions = {}
    for part in ("train", "validation", "test"):
        rows = [m for m in members if m.get("partition") == part]
        if not rows:
            continue
        x = np.array([[m["numeric12"][name] for name in NUMERIC_FIELDS] for m in rows])
        partitions[part] = {
            "x": x,
            "y": np.array([m["label"] for m in rows]),
            "events": np.array([m["event_epoch"] for m in rows], dtype=np.int64),
            "identities": np.array(
                [m["canonical_repository"] + ":" + m["commit_id"] for m in rows]
            ),
            "effort": x[:, 0] + x[:, 1],
        }
    return partitions


def train_preset(spec, asset_directory, source_hash, *, tick=None):
    """按预置规格在给定资产上训练估计器（标准化在训练分区拟合、校准按既有规则）。"""
    members, _metadata, _digest = research.prepare(
        asset_directory, source_hash, PRESET_RATIOS, tick=tick
    )
    partitions = preset_partitions(members)
    estimator, _calibrator, _threshold, _partition, _first, _second = train.fit_model(
        partitions, spec["plugin"], spec["params"], spec["seed"]
    )
    return estimator


def ensure_presets(engine, *, store=None, root=None, tick=None):
    """注册缺失的预置基线条目（按名称 + 代码版本幂等），返回本次注册的条目数。

    训练在内置资产上进行；预置条目没有所有者，只能由管理员移除。同一代码
    版本内重复调用直接跳过，版本升级后新条目按新版本名注册、旧条目保留但
    不再命中新请求的契约校验（code_sha 不同）。
    """
    store = store or research.STORE
    root = root or pool.POOL_ROOT
    code_sha = provenance()["code_sha"]
    registered = []
    for spec in preset_specs():
        # 每个预置规格一个独立预算窗口（Budget 单实例上限 180 秒），tick 由
        # 调用方传入以叠加整体截止时间。
        budget = assets.Budget(tick=tick)
        budget.check()
        with Session(engine) as db:
            existing = db.scalar(
                select(ModelPoolEntry).where(
                    ModelPoolEntry.source_kind == "preset",
                    ModelPoolEntry.name == spec["name"],
                    ModelPoolEntry.status == "active",
                )
            )
        if existing:
            continue
        asset_directory, loaded = builtin_asset(store, tick=budget.check)
        estimator = train_preset(
            spec, asset_directory, loaded["manifest"]["content_hash"], tick=budget.check
        )
        contract_value = pool.contract(
            spec["plugin"], spec["params"], spec["features"], spec["forms"]
        )
        entry_id = pool.new_entry_id()
        record = pool.seal_estimator(root, entry_id, estimator, contract_value, tick=budget.check)
        with Session(engine) as db, db.begin():
            existing = db.scalar(
                select(ModelPoolEntry).where(
                    ModelPoolEntry.source_kind == "preset",
                    ModelPoolEntry.name == spec["name"],
                    ModelPoolEntry.status == "active",
                )
            )
            if existing:
                continue
            db.add(
                ModelPoolEntry(
                    id=entry_id,
                    name=spec["name"],
                    source_kind="preset",
                    source_run_id=None,
                    owner_id=None,
                    plugin=spec["plugin"],
                    params_json=spec["params"],
                    features_json=spec["features"],
                    forms_json=spec["forms"],
                    code_sha=code_sha,
                    artifact_uri=record["uri"],
                    artifact_sha256=record["sha256"],
                    artifact_size=record["bytes"],
                    status="active",
                )
            )
            db.flush()
        registered.append(spec["name"])
    return registered
