"""Sealed estimator pool shared by training requests (four-tuple exact reuse).

池条目保存「训练好的估计器参数」与训练契约（插件、超参、特征集、特征形式）。
复用训练跳过估计器拟合，但标准化统计、校准器与门槛一律在新数据集上重做。
池目录里每个条目是一个密封目录：estimator.skops（拟合好的估计器）+ manifest.json
（契约与工件摘要的密封清单），数据库行只保存引用与摘要，加载时逐项校验。
"""

import shutil
import uuid

import skops.io as sio

from packages.mining import public_assets as assets
from packages.modeling.policy import ModelError

POOL_ROOT = assets.ROOT / "runtime/model-pool"
SOURCE_KINDS = ("preset", "run")
MAX_ESTIMATOR_BYTES = 256 * 1024**2


def contract(plugin, params, features, forms):
    """把训练契约归一化成四元组字典，用于匹配、密封与展示。"""
    return {
        "plugin": plugin,
        "params": dict(params),
        "features": list(features),
        "forms": dict(forms),
    }


def matches(entry, plugin, params, features, forms):
    """四元组精确匹配；entry 可以是数据库行（params_json 等列）或普通字典。"""

    def value(*names):
        for name in names:
            if isinstance(entry, dict):
                if name in entry:
                    return entry[name]
            elif hasattr(entry, name):
                return getattr(entry, name)
        return None

    params_value = value("params_json", "params")
    features_value = value("features_json", "features")
    forms_value = value("forms_json", "forms")
    return (
        value("plugin") == plugin
        and dict(params_value) == dict(params)
        and list(features_value) == list(features)
        and dict(forms_value) == dict(forms)
    )


def seal_estimator(root, entry_id, estimator, contract_value, *, tick=None):
    """把拟合好的估计器密封进池目录，返回工件记录 {uri, sha256, bytes}。"""
    root = assets.safe_path(root)
    directory = assets.safe_path(root / entry_id)
    directory.mkdir(parents=True, exist_ok=False)
    budget = assets.Budget(tick=tick, seconds=120)
    try:
        path = directory / "estimator.skops"
        sio.dump(estimator, path)
        if sio.get_untrusted_types(file=path):
            raise ModelError("MODEL_POOL_ARTIFACT_UNTRUSTED")
        digest = assets.file_digest(path, budget.check)
        if digest["bytes"] > MAX_ESTIMATOR_BYTES:
            raise ModelError("MODEL_POOL_ARTIFACT_LIMIT")
        manifest = assets.sealed(
            {
                "contract": contract_value,
                "estimator": digest,
            }
        )
        assets.write_json(directory / "manifest.json", manifest)
        return {"uri": f"{entry_id}/estimator.skops", **digest}
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def load_estimator(root, entry, *, tick=None):
    """按数据库行的工件摘要加载池估计器；摘要不一致或工件损坏一律拒绝。"""
    root = assets.safe_path(root)
    directory = assets.safe_path(root / entry["id"])
    budget = assets.Budget(tick=tick, seconds=120)
    manifest = assets.read_json(
        directory / "manifest.json", budget.check, maximum=64 * 1024
    )
    if manifest.get("estimator", {}).get("sha256") != entry["artifact_sha256"]:
        raise ModelError("MODEL_POOL_ARTIFACT_DRIFT", 409)
    path = directory / "estimator.skops"
    digest = assets.file_digest(path, budget.check)
    if digest["sha256"] != entry["artifact_sha256"] or digest["bytes"] != entry["artifact_size"]:
        raise ModelError("MODEL_POOL_ARTIFACT_DRIFT", 409)
    estimator = sio.load(path, trusted=[])
    if sio.get_untrusted_types(file=path):
        raise ModelError("MODEL_POOL_ARTIFACT_UNTRUSTED")
    return estimator, manifest


def extract_run_estimator(run_directory, *, tick=None):
    """从一次训练产物的 model.skops 里取出估计器（去掉校准器与门槛）。

    训练产物的 bundle 同时含校准器与门槛，池条目只保存估计器参数：
    校准器与门槛是逐数据集拟合的，直接复用会破坏防泄漏纪律。
    """
    path = assets.safe_path(run_directory / "model.skops")
    bundle = sio.load(path, trusted=[])
    if sio.get_untrusted_types(file=path):
        raise ModelError("MODEL_POOL_ARTIFACT_UNTRUSTED")
    estimator = bundle.get("model")
    if estimator is None or not hasattr(estimator, "predict_proba"):
        raise ModelError("MODEL_POOL_SOURCE_INVALID")
    return estimator


def new_entry_id():
    return uuid.uuid4().hex
