"""Prediction kernel unit checks: diff parsing, level thresholds, digest parity and shapes."""

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.mining.features import (
    FEATURE_VERSION,
    CommitInput,
    FileDelta,
    Snapshot,
    compute,
)
from packages.prediction.policy import DISCLAIMER, MODEL_COLUMNS, PredictionError
from packages.prediction.service import (
    PredictionService,
    _level,
    _parse_unified_diff,
    _source_digest,
)
from packages.tasks.service import TaskError

CALIBRATION = "validation-chronological-sigmoid-logit-v1"

BASE = CommitInput("a" * 40, (), datetime(2026, 1, 1), "b" * 64, ())

DIFF = """diff --git a/src/util.py b/src/util.py
--- a/src/util.py
+++ b/src/util.py
@@ -1,3 +1,4 @@
 def clamp(value):
     return max(0, value)
+    return min(9, value)
@@ -10,2 +11,3 @@
 def second():
     return 2
+    return 3
diff --git a/src/added.py b/src/added.py
new file mode 100644
--- /dev/null
+++ b/src/added.py
@@ -0,0 +1,2 @@
+print("added")
+print("again")
diff --git a/src/gone.py b/src/gone.py
deleted file mode 100644
--- a/src/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-print("gone")
-print("lost")
"""


def test_diff_parser_hunk_counts_and_modes():
    deltas = _parse_unified_diff(DIFF)
    assert [d.change_type for d in deltas] == ["M", "A", "D"]
    modified, added, deleted = deltas
    assert modified.old_path == modified.new_path == "src/util.py"
    assert modified.insertions == 2 and modified.deletions == 0
    assert modified.old_loc == 2 + 2  # 两个 hunk 各两行上下文，无删除行
    assert added.old_path is None and added.new_path == "src/added.py"
    assert added.old_mode == "000000" and added.new_mode == "100644"
    assert added.insertions == 2 and added.old_loc == 0
    assert deleted.new_path is None and deleted.old_path == "src/gone.py"
    assert deleted.new_mode == "000000" and deleted.deletions == 2


def test_diff_parser_rejects_empty_and_non_diff():
    for text in ("", "not a diff at all\n", "   "):
        with pytest.raises(PredictionError) as failure:
            _parse_unified_diff(text)
        assert failure.value.code == "PRED_INVALID_INPUT"
    overlong = "\n".join(
        "diff --git a/f{i} b/f{i}\n--- a/f{i}\n+++ b/f{i}\n@@ -0,0 +1 @@\n+line"
        for i in range(201)
    )
    with pytest.raises(PredictionError) as failure:
        _parse_unified_diff(overlong)
    assert failure.value.code == "PRED_INPUT_LIMIT"


def test_level_threshold_boundaries():
    threshold = 0.5
    assert _level(0.5, threshold) == "high"
    assert _level(0.99, threshold) == "high"
    assert _level(0.25, threshold) == "medium"
    assert _level(0.4999, threshold) == "medium"
    assert _level(0.2499, threshold) == "low"
    assert _level(0.0, threshold) == "low"


def test_candidate_diff_identity_and_timestamp():
    text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    candidate = PredictionService._candidate_diff(text, BASE.sha, {BASE.sha: BASE}, "actor-1")
    canonical = text.replace("\r\n", "\n")
    assert candidate.sha == hashlib.sha256(canonical.encode()).hexdigest()
    assert candidate.parents == (BASE.sha,)
    assert candidate.event_time == BASE.event_time + timedelta(microseconds=1)
    assert candidate.identity_key == hashlib.sha256(b"user:actor-1").hexdigest()
    with pytest.raises(PredictionError) as failure:
        PredictionService._candidate_diff("", BASE.sha, {BASE.sha: BASE}, "actor-1")
    assert failure.value.code == "PRED_INVALID_INPUT"


def build_history():
    root = CommitInput(
        "c" * 40,
        (),
        datetime(2025, 1, 1),
        "d" * 64,
        (
            FileDelta(
                old_path=None,
                new_path="src/core.py",
                change_type="A",
                insertions=10,
                deletions=0,
                old_loc=0,
                old_mode="000000",
            ),
        ),
    )
    base = CommitInput(
        "a" * 40,
        ("c" * 40,),
        datetime(2025, 2, 1),
        "e" * 64,
        (
            FileDelta(
                old_path="src/core.py",
                new_path="src/core.py",
                change_type="M",
                insertions=2,
                deletions=1,
                old_loc=9,
            ),
        ),
    )
    return {commit.sha: commit for commit in (root, base)}


def test_source_digest_matches_kernel_and_fix_defaults():
    history = build_history()
    candidate = PredictionService._candidate_diff(
        "diff --git a/src/core.py b/src/core.py\n--- a/src/core.py\n+++ b/src/core.py\n"
        "@@ -1 +1 @@\n-old\n+new\n",
        "a" * 40,
        history,
        "actor-2",
    )
    snapshot = Snapshot("repo-1", candidate.sha, tuple([*history.values(), candidate]))
    row = compute(snapshot, "candidate")[-1]
    assert row.source_digest == _source_digest(snapshot, "candidate")
    assert dict(row.values)["FIX"] == 0
    assert dict(row.reasons)["FIX"] == "candidate_fix_default"
    # 十二维模型合同全部可重算，没有任何缺列。
    assert all(value is not None for value in dict(row.values).values())


def test_lr_explanation_contribution_math():
    np = pytest.importorskip("numpy")
    scale = np.array([2.0] * 12)
    coefficients = np.array([0.4, -0.6, 0.2] + [0.1] * 9)
    model = SimpleNamespace(
        named_steps={
            "standardscaler": SimpleNamespace(scale_=scale),
            "logisticregression": SimpleNamespace(coef_=coefficients.reshape(1, -1)),
        }
    )
    bundle = {"model": model}
    matrix = np.array([[1.0, 2.0, -3.0] + [0.5] * 9])
    method, factors = PredictionService._explain(bundle, matrix)
    assert method == "lr-scaled-coefficient-v1"
    expected = dict(
        zip(MODEL_COLUMNS, (matrix[0] * (coefficients / scale)).tolist(), strict=True)
    )
    for factor in factors[0]:
        contribution = expected[factor["feature"]]
        assert factor["contribution"] == pytest.approx(contribution, abs=1e-6)  # 贡献保留六位
        assert factor["direction"] == ("increase" if contribution >= 0 else "decrease")
    magnitudes = [abs(factor["contribution"]) for factor in factors[0]]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert factors[0][0]["feature"] == "ld"  # 2.0 * -0.6 / 2.0 的绝对值最大


def test_result_risk_levels_effort_and_missing(monkeypatch):
    def fake_predict(bundle, row, explain):
        factors = [
            {"feature": column, "value": value, "contribution": 0.01, "direction": "increase"}
            for column, value in zip(bundle["features"], row, strict=True)
        ]
        return bundle["probability"], "lr-scaled-coefficient-v1", [factors]

    monkeypatch.setattr(PredictionService, "_predict", staticmethod(fake_predict))
    service = PredictionService(
        SimpleNamespace(repository_storage_root=Path(".")), SimpleNamespace(engine=None)
    )
    candidate = CommitInput(
        hashlib.sha256(b"diff").hexdigest(),
        (BASE.sha,),
        BASE.event_time + timedelta(microseconds=1),
        "f" * 64,
        (),
    )
    values = {product: 300.0 for product in ("LA", "LD", "NF", "ND", "NS", "Entropy")}
    values.update({product: 1.0 for product in ("NDEV", "AGE", "NUC", "EXP", "REXP", "SEXP")})
    bundle = {"threshold": 0.5, "probability": 0.7, "features": list(values)}
    result = service._result(bundle, dict(values), {}, candidate)
    assert result["risk_level"] == "high"
    assert result["effort"] == 600 and result["oversized"] is True
    assert result["risk_density"] == pytest.approx(0.7 / 600, abs=1e-6)  # 定点小数保留六位
    bundle["probability"] = 0.3
    assert service._result(bundle, dict(values), {}, candidate)["risk_level"] == "medium"
    bundle["probability"] = 0.2
    assert service._result(bundle, dict(values), {}, candidate)["risk_level"] == "low"
    missing = dict(values)
    missing["NDEV"] = None
    with pytest.raises(PredictionError) as failure:
        service._result(bundle, missing, {"NDEV": "ancestor_time_inversion"}, candidate)
    assert failure.value.code == "PRED_FEATURE_UNAVAILABLE"
    assert "NDEV" in failure.value.reason and "ancestor_time_inversion" in failure.value.reason


def test_single_response_contract_shape():
    row = SimpleNamespace(
        id="p-1",
        probability=0.73,
        risk_level="high",
        effort=12,
        risk_density=0.06,
        threshold=0.5,
        explanation_json={"factors": [{"feature": f"f{i}"} for i in range(7)]},
    )
    point = SimpleNamespace(model_run_id="m-1", feature_version=FEATURE_VERSION)
    response = PredictionService._single_response(row, point)
    assert set(response) == {
        "prediction_id",
        "model_run_id",
        "probability",
        "calibration",
        "risk_level",
        "effort",
        "risk_density",
        "threshold",
        "feature_version",
        "top_factors",
        "limitations",
    }
    assert response["prediction_id"] == "p-1"
    assert response["calibration"] == CALIBRATION
    assert len(response["top_factors"]) == 5
    assert response["limitations"] == [DISCLAIMER]


def test_queue_item_shape():
    row = SimpleNamespace(
        id="p-2",
        candidate_sha="a" * 40,
        event_time=datetime(2026, 1, 1),
        probability=0.5,
        risk_level="medium",
        effort=9,
        risk_density=0.05,
        oversized=False,
        explanation_json={"factors": [{"feature": f"f{i}"} for i in range(5)]},
    )
    item = PredictionService._queue_item(row)
    assert item["event_time"] == "2026-01-01T00:00:00.000000Z"
    assert len(item["top_factors"]) == 3
    assert item["oversized"] is False


def test_create_rejects_before_touching_database(monkeypatch):
    service = PredictionService(
        SimpleNamespace(repository_storage_root=Path(".")), SimpleNamespace(engine=None)
    )
    user = SimpleNamespace(id="u-1")
    with pytest.raises(TaskError) as failure:
        service.create_single(user, "bad key", {}, "r-1")
    assert failure.value.code == "TASK_INVALID_INPUT"
    request = {"window_days": 0, "max_candidates": 3}
    with pytest.raises(PredictionError) as failure:
        service.create_batch(user, "good-key", request, "r-1")
    assert failure.value.code == "PRED_INPUT_LIMIT"
    request = {"window_days": 91, "max_candidates": 3}
    with pytest.raises(PredictionError) as failure:
        service.create_batch(user, "good-key", request, "r-1")
    assert failure.value.code == "PRED_INPUT_LIMIT"
    request = {"window_days": 30, "max_candidates": 501}
    with pytest.raises(PredictionError) as failure:
        service.create_batch(user, "good-key", request, "r-1")
    assert failure.value.code == "PRED_INPUT_LIMIT"
