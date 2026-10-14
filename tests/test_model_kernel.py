"""Independent metric oracles and partition isolation; fixtures are not effect evidence."""

import numpy as np
import pytest

from packages.modeling import metrics, policy
from packages.modeling.train import calibration_split, fit_model, sigmoid_input, threshold_for


def test_hand_calculated_effort_and_ties():
    value = metrics.effort_metrics(
        [1, 0, 1, 0], [0.9, 0.8, 0.7, 0.6], [1, 1, 1, 1], np.arange(4), ["a", "b", "c", "d"]
    )
    assert value["pofb20"] == 0.5 and value["ifa"] == 0
    assert value["popt"] == 0.75
    tied = metrics.effort_metrics([0, 1], [0.5, 0.5], [1, 1], [0, 0], ["a", "b"])
    assert tied["ifa"] == 1 and tied["pofb20"] == 0 and tied["popt"] == 0


def test_metrics_and_undefined_values():
    result = metrics.evaluate(
        [0, 1, 0, 1], [0.1, 0.8, 0.6, 0.7], 0.5, [1] * 4, np.arange(4), ["a", "b", "c", "d"]
    )
    assert result["confusion_matrix"] == [[1, 1], [0, 2]]
    assert result["precision"] == 2 / 3 and result["recall"] == 1
    assert result["f1"] == 0.8 and result["roc_auc"] == 1
    assert result["brier"] == pytest.approx(0.125)
    single = metrics.evaluate([0, 0], [0.2, 0.3], 0.5, [0, 0], [0, 1], ["a", "b"])
    assert single["roc_auc"] is single["pr_auc"] is single["mcc"] is None
    assert single["undefined_reason"] == "single_class"
    assert single["effort"]["popt"] is None


def test_cross_repository_tie_uses_sha_before_repository():
    value = metrics.effort_metrics([0, 1], [0.5, 0.5], [1, 1], [0, 0], ["z:000", "a:fff"])
    assert value["ifa"] == 1 and value["pofb20"] == 0 and value["popt"] == 0


@pytest.mark.parametrize(
    "plugin,params,seed",
    [
        ("lr", {"c": True}, 42),
        ("lr", {"max_iter": 20}, 42),
        ("rf", {"n_jobs": 8}, 42),
        ("rf", {"max_depth": None}, 42),
        ("rf", {"n_estimators": 300}, 42),
        ("lr", {"c": float("nan")}, 42),
        ("lr", {}, True),
        ("unknown", {}, 42),
        pytest.param("rf", {"n_estimators": 10**1000}, 42, id="oversized-int"),
    ],
)
def test_parameter_resource_boundaries(plugin, params, seed):
    with pytest.raises(policy.ModelError, match="MODEL_INVALID_INPUT"):
        policy.parameters(plugin, params, seed)


def test_calibration_time_groups_and_class_refusal():
    a, b = calibration_split(np.array([0, 0, 1, 1, 2, 2, 3, 3]), np.tile([0, 1], 4))
    assert not np.any(a & b) and np.all(a | b)
    assert a.tolist() == [True] * 4 + [False] * 4
    with pytest.raises(policy.ModelError, match="MODEL_CALIBRATION_UNUSABLE"):
        calibration_split(np.zeros(8), np.tile([0, 1], 4))
    with pytest.raises(policy.ModelError, match="MODEL_CALIBRATION_UNUSABLE"):
        calibration_split(np.arange(8), np.array([0] * 4 + [1] * 4))
    assert threshold_for([0, 0], [0.1, 0.1]) == 0.95


@pytest.mark.parametrize("plugin", ["lr", "rf"])
def test_test_partition_cannot_change_fit_calibration_or_threshold(plugin):
    rng = np.random.default_rng(17)
    parts = {
        name: {"x": rng.random((80, 12)), "y": np.tile([0, 1], 40), "events": np.arange(80)}
        for name in ("train", "validation", "test")
    }
    params = policy.parameters(plugin, {}, 42)
    first = fit_model(parts, plugin, params, 42)
    parts["test"] = {"x": np.full((80, 12), 1e9), "y": np.zeros(80), "events": np.zeros(80)}
    second = fit_model(parts, plugin, params, 42)
    assert first[2] == second[2]
    assert np.array_equal(first[1].coef_, second[1].coef_)
    assert np.array_equal(
        first[0].predict_proba(parts["train"]["x"]), second[0].predict_proba(parts["train"]["x"])
    )
    if plugin == "lr":
        assert np.array_equal(first[0].steps[0][1].mean_, parts["train"]["x"].mean(axis=0))
    assert np.isfinite(sigmoid_input([0, 1])).all()


def test_time_block_interval_reproducibility():
    y, p = np.tile([0, 1], 10), np.tile([0.2, 0.8], 10)
    args = (
        y,
        p,
        0.5,
        np.ones(20),
        np.repeat(np.arange(10), 2),
        np.array([str(i) for i in range(20)]),
        42,
    )
    result = metrics.intervals(*args, repeats=20)
    assert result == metrics.intervals(*args, repeats=20)
    assert result["metrics"]["pr_auc"]["lower"] == 1
