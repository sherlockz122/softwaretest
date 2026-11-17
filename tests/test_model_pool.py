"""Model pool kernel: four-tuple matching, estimator sealing, run extraction, presets."""

from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pytest
import skops.io as sio
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.public_dataset import NUMERIC_FIELDS
from packages.modeling import pool, presets, train
from packages.modeling.policy import DEFAULT_FORMS, PLUGINS, ModelError
from tests.dataset_support import fixture_asset


@pytest.fixture
def runtime_tmp():
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="pool-") as path:
        yield Path(path)


def lr_entry(**overrides):
    entry = {
        "id": pool.new_entry_id(),
        "plugin": "lr",
        "params_json": dict(PLUGINS["lr"]["params"]),
        "features_json": list(NUMERIC_FIELDS),
        "forms_json": {n: DEFAULT_FORMS["lr"] for n in NUMERIC_FIELDS},
    }
    entry.update(overrides)
    return entry


def fitted_lr(seed=3):
    x = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 3.0], [3.0, 2.0], [4.0, 5.0], [5.0, 4.0]])
    y = np.array([0, 0, 0, 1, 1, 1])
    return LogisticRegression(max_iter=500, random_state=seed).fit(x, y)


def test_four_tuple_matching_is_exact_per_dimension():
    contract_value = pool.contract(
        "lr", PLUGINS["lr"]["params"], list(NUMERIC_FIELDS),
        {n: DEFAULT_FORMS["lr"] for n in NUMERIC_FIELDS},
    )
    entry = lr_entry()
    assert pool.matches(entry, **contract_value)
    assert not pool.matches(
        entry,
        "rf",
        **{k: v for k, v in contract_value.items() if k != "plugin"},
    )
    assert not pool.matches(
        entry,
        **pool.contract(
            "lr",
            {"c": 2.0, "max_iter": 500},
            contract_value["features"],
            contract_value["forms"],
        ),
    )
    assert not pool.matches(
        entry,
        **pool.contract(
            "lr",
            contract_value["params"],
            contract_value["features"][1:],
            contract_value["forms"],
        ),
    )
    forms = dict(contract_value["forms"], la="raw")
    assert not pool.matches(
        entry, **pool.contract("lr", contract_value["params"], contract_value["features"], forms)
    )


def test_seal_and_load_roundtrip(runtime_tmp):
    estimator = fitted_lr()
    contract_value = pool.contract("lr", PLUGINS["lr"]["params"], list(NUMERIC_FIELDS),
                                   {n: "standardize" for n in NUMERIC_FIELDS})
    entry_id = pool.new_entry_id()
    record = pool.seal_estimator(runtime_tmp, entry_id, estimator, contract_value)
    entry = lr_entry(
        id=entry_id, artifact_sha256=record["sha256"], artifact_size=record["bytes"]
    )
    loaded, manifest = pool.load_estimator(runtime_tmp, entry)
    assert manifest["contract"] == contract_value
    assert loaded.predict_proba([[1.0, 1.0]]).shape == (1, 2)
    assert np.array_equal(loaded.coef_, estimator.coef_)


def test_tampered_or_mismatched_artifact_rejected(runtime_tmp):
    estimator = fitted_lr()
    contract_value = pool.contract("lr", PLUGINS["lr"]["params"], list(NUMERIC_FIELDS),
                                   {n: "standardize" for n in NUMERIC_FIELDS})
    entry_id = pool.new_entry_id()
    record = pool.seal_estimator(runtime_tmp, entry_id, estimator, contract_value)
    entry = lr_entry(
        id=entry_id, artifact_sha256=record["sha256"], artifact_size=record["bytes"]
    )
    with (runtime_tmp / entry_id / "estimator.skops").open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(ModelError) as exc:
        pool.load_estimator(runtime_tmp, entry)
    assert exc.value.code == "MODEL_POOL_ARTIFACT_DRIFT"
    wrong = lr_entry(
        id=entry_id, artifact_sha256="0" * 64, artifact_size=record["bytes"]
    )
    with pytest.raises(ModelError) as exc:
        pool.load_estimator(runtime_tmp, wrong)
    assert exc.value.code == "MODEL_POOL_ARTIFACT_DRIFT"


def test_extract_run_estimator_keeps_predictions_and_rejects_missing_model(runtime_tmp):
    estimator = fitted_lr(seed=5)
    calibrator = LogisticRegression(C=1e6, max_iter=200, random_state=5).fit(
        [[-1.0], [0.0], [1.0], [2.0]], [0, 0, 1, 1]
    )
    run = runtime_tmp / "run"
    run.mkdir()
    sio.dump({"model": estimator, "calibrator": calibrator, "threshold": 0.5}, run / "model.skops")
    extracted = pool.extract_run_estimator(run)
    assert extracted.predict_proba([[1.0, 1.0]])[0].tolist() == pytest.approx(
        estimator.predict_proba([[1.0, 1.0]])[0].tolist()
    )
    sio.dump({"calibrator": calibrator, "threshold": 0.5}, run / "model.skops")
    with pytest.raises(ModelError) as exc:
        pool.extract_run_estimator(run)
    assert exc.value.code == "MODEL_POOL_SOURCE_INVALID"


def test_builtin_asset_selection_prefers_apachejit_and_skips_others(runtime_tmp):
    store = runtime_tmp / "store"
    directory, source = fixture_asset(runtime_tmp, rows=80, store=store)
    (store / ("a" * 64)).mkdir()
    (store / "not-an-asset.txt").write_text("x", encoding="utf-8")
    found, loaded = presets.builtin_asset(store)
    assert found.name == directory.name
    assert loaded["manifest"]["asset_id"] == source["manifest"]["asset_id"]


def test_preset_training_fits_lr_and_rf_on_prepared_members(runtime_tmp):
    store = runtime_tmp / "store"
    directory, source = fixture_asset(runtime_tmp, rows=200, store=store)
    lr_spec, rf_spec = presets.preset_specs()
    assert len(lr_spec["features"]) == 12 and lr_spec["plugin"] == "lr"
    members, _metadata, _digest = research.prepare(
        directory, source["manifest"]["content_hash"], presets.PRESET_RATIOS
    )
    partitions = presets.preset_partitions(members)
    assert set(partitions) == {"train", "validation", "test"}
    lr_estimator, _cal, threshold, _part, _first, _second = train.fit_model(
        partitions, "lr", lr_spec["params"], lr_spec["seed"]
    )
    assert isinstance(lr_estimator, LogisticRegression)
    assert 0.05 <= threshold <= 0.95
    rf_estimator, _cal, _threshold, _part, _first, _second = train.fit_model(
        partitions, "rf", rf_spec["params"], rf_spec["seed"]
    )
    assert isinstance(rf_estimator, RandomForestClassifier)
    x = partitions["test"]["x"]
    assert lr_estimator.predict_proba(x).shape == (x.shape[0], 2)
    assert rf_estimator.predict_proba(x).shape == (x.shape[0], 2)


def test_preset_partitions_match_fit_requirements(runtime_tmp):
    store = runtime_tmp / "store"
    directory, source = fixture_asset(runtime_tmp, rows=120, store=store)
    members, _metadata, _digest = research.prepare(
        directory, source["manifest"]["content_hash"], presets.PRESET_RATIOS
    )
    partitions = presets.preset_partitions(members)
    for part, data in partitions.items():
        assert data["x"].shape[1] == 12
        assert len(data["y"]) == len(data["events"]) == len(data["identities"])
        assert set(data["y"]) == {0, 1}
    for spec in presets.preset_specs():
        estimator = presets.train_preset(
            spec, directory, source["manifest"]["content_hash"]
        )
        assert hasattr(estimator, "predict_proba")
