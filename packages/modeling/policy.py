"""Lightweight contracts shared by API and the supervised training process."""

import hashlib
import math
from importlib.metadata import version
from pathlib import Path

from packages.mining import public_assets as assets
from packages.mining.public_dataset import NUMERIC_FIELDS
from packages.mining.source_evidence import content_hash

VERSION = "research-lr-rf-v3"
PURPOSE = "published_binary_12_v1"
OUTPUT = assets.ROOT / "runtime/models"
MAX_SECONDS = 900
MAX_RSS = 2 * 1024**3
FILES = ("model.skops", "report.json", "probabilities.csv")
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
