"""Lightweight contracts shared by API and the supervised training process."""

import hashlib
import math
from importlib.metadata import version
from pathlib import Path

from packages.mining import public_assets as assets
from packages.mining.public_dataset import NUMERIC_FIELDS
from packages.mining.source_evidence import content_hash

VERSION = "research-lr-rf-v4"
PURPOSE = "published_binary_12_v1"
OUTPUT = assets.ROOT / "runtime/models"
MAX_SECONDS = 900
MAX_RSS = 2 * 1024**3
FILES = ("model.skops", "report.json", "probabilities.csv")
FEATURE_FORMS = ("raw", "log1p", "standardize")
DEFAULT_FORMS = {"lr": "standardize", "rf": "raw"}
PLUGINS = {
    "lr": {"name": "Logistic Regression", "params": {"c": 1.0, "max_iter": 500}},
    "rf": {
        "name": "Random Forest",
        "params": {"n_estimators": 100, "max_depth": 16, "min_samples_leaf": 2},
    },
}


class ModelError(ValueError):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def parameters(plugin, values, seed):
    if plugin not in PLUGINS or type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise ModelError("MODEL_INVALID_INPUT")
    if not isinstance(values, dict) or set(values) - set(PLUGINS[plugin]["params"]):
        raise ModelError("MODEL_INVALID_INPUT")
    result = PLUGINS[plugin]["params"] | values
    bounds = (
        {"c": (0.001, 1000, False), "max_iter": (50, 1000, True)}
        if plugin == "lr"
        else {
            "n_estimators": (10, 256, True),
            "max_depth": (2, 32, True),
            "min_samples_leaf": (1, 100, True),
        }
    )
    for key, (lower, upper, integral) in bounds.items():
        v = result[key]
        if (
            type(v) not in ((int,) if integral else (int, float))
            or not lower <= v <= upper
            or not math.isfinite(v)
        ):
            raise ModelError("MODEL_INVALID_INPUT")
    return result


def feature_plan(plugin, features, forms):
    """把用户给的特征子集与形式归一化成确定顺序的契约。

    features 缺省为全部字段、顺序固定为数据集字段顺序；forms 缺省按插件补全
    （逻辑回归标准化、随机森林原始值，与旧行为一致），键只能属于已选特征。
    """
    if plugin not in PLUGINS:
        raise ModelError("MODEL_INVALID_INPUT")
    chosen = list(NUMERIC_FIELDS) if features is None else features
    if (
        not isinstance(chosen, list)
        or not chosen
        or len(set(chosen)) != len(chosen)
        or any(type(name) is not str or name not in NUMERIC_FIELDS for name in chosen)
    ):
        raise ModelError("MODEL_FEATURE_INVALID")
    chosen = [name for name in NUMERIC_FIELDS if name in chosen]
    given = {} if forms is None else forms
    if (
        not isinstance(given, dict)
        or set(given) - set(chosen)
        or any(name not in FEATURE_FORMS for name in given.values())
        or any(type(name) is not str for name in given.values())
    ):
        raise ModelError("MODEL_FEATURE_INVALID")
    default = DEFAULT_FORMS[plugin]
    return {
        "features": chosen,
        "forms": {name: given.get(name, default) for name in chosen},
    }


def provenance():
    paths = [*Path(__file__).parent.glob("*.py"), assets.ROOT / "uv.lock"]
    sources = {
        p.relative_to(assets.ROOT).as_posix(): hashlib.sha256(
            p.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for p in sorted(paths)
    }
    return {
        "version": VERSION,
        "code_sha": content_hash(sources),
        "code_sha_kind": "canonical-source-and-lock-sha256",
        "sources": sources,
        "dependencies": {
            p: version(p)
            for p in (
                "scikit-learn",
                "numpy",
                "scipy",
                "skops",
                "psutil",
                "joblib",
                "threadpoolctl",
            )
        },
        "features": list(NUMERIC_FIELDS),
    }
