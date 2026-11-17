"""Trusted bounded CPU training entry point, launched only by the supervisor."""

import csv
import os
import threading
import time
import warnings
from pathlib import Path

import numpy as np
import psutil
import skops.io as sio
from sklearn.ensemble import RandomForestClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
from sklearn.preprocessing import StandardScaler

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.source_evidence import content_hash
from packages.modeling import metrics, policy, pool
from packages.modeling.policy import ModelError, provenance


def load_partition(path):
    if path.stat().st_size > assets.MAX_BYTES:
        raise ModelError("MODEL_DATA_LIMIT")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != research.CSV_FIELDS:
            raise ModelError("MODEL_DATA_DRIFT")
        rows = list(reader)
    if not 4 <= len(rows) <= assets.MAX_ROWS:
        raise ModelError("MODEL_DATA_LIMIT")
    x = np.array([[float(r[k]) for k in research.NUMERIC_FIELDS] for r in rows])
    y = np.array([int(r["label"]) for r in rows])
    events = np.array([int(r["event_epoch"]) for r in rows], dtype=np.int64)
    if not np.isfinite(x).all() or (x < 0).any() or set(y) != {0, 1} or (np.diff(events) < 0).any():
        raise ModelError("MODEL_DATA_INVALID")
    identities = np.array([r["canonical_repository"] + ":" + r["commit_id"] for r in rows])
    return {
        "x": x,
        "y": y,
        "events": events,
        "identities": identities,
        "effort": x[:, 0] + x[:, 1],
        "rows": rows,
    }


def feature_matrix(partitions, features, forms):
    """按所选特征与形式构造各分区矩阵；标准化统计只在训练分区拟合。

    列序固定为数据集字段顺序；log1p 逐值变换；standardize 直接复用
    StandardScaler（ddof=0、零方差列按方差 1），保证缺省配置与旧管线数值一致。
    """
    index = {name: i for i, name in enumerate(research.NUMERIC_FIELDS)}
    scaler_names = [name for name in features if forms[name] == "standardize"]
    scaler, stats = None, {}
    if scaler_names:
        scaler = StandardScaler().fit(
            partitions["train"]["x"][:, [index[name] for name in scaler_names]]
        )
        stats = {
            name: {"mean": float(scaler.mean_[i]), "scale": float(scaler.scale_[i])}
            for i, name in enumerate(scaler_names)
        }
    matrices = {}
    for p, d in partitions.items():
        x = np.empty((d["x"].shape[0], len(features)), dtype=float)
        scaled = None
        if scaler is not None:
            scaled = scaler.transform(d["x"][:, [index[n] for n in scaler_names]])
        for j, name in enumerate(features):
            form = forms[name]
            column = d["x"][:, index[name]]
            if form == "log1p":
                x[:, j] = np.log1p(column)
            elif form == "standardize":
                x[:, j] = scaled[:, scaler_names.index(name)]
            else:
                x[:, j] = column
        matrices[p] = x
    return matrices, stats


def calibration_split(events, labels):
    unique = np.unique(events)
    if len(unique) < 2:
        raise ModelError("MODEL_CALIBRATION_UNUSABLE")
    boundary = unique[len(unique) // 2]
    first, second = events < boundary, events >= boundary
    if any(set(labels[m]) != {0, 1} for m in (first, second)):
        raise ModelError("MODEL_CALIBRATION_UNUSABLE")
    return first, second


def sigmoid_input(p):
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(clipped / (1 - clipped)).reshape(-1, 1)


def threshold_for(y, p):
    p = np.asarray(p)
    candidates = [i / 100 for i in range(5, 96)]
    return max(candidates, key=lambda t: (f1_score(y, p >= t, zero_division=0), t))


def fit_model(partitions, plugin, params, seed, stage=lambda _: None, *, reuse_estimator=None):
    """训练 + 校准/定门槛。

    验证集可选：没有验证分区时，用训练分区按时间切成前后两段来做校准和
    定门槛（样本内做法，报告里会显式标注 calibration_partition=train）。
    reuse_estimator 非空时跳过估计器拟合（复用池估计器参数），标准化统计、
    校准器与门槛仍全部在新数据集上重做。
    """
    train = partitions["train"]
    if "validation" in partitions:
        calibration_partition = "validation"
        calibration_data = partitions["validation"]
        stage_name = "calibrating_validation"
    else:
        calibration_partition = "train"
        calibration_data = train
        stage_name = "calibrating_train_split"
    first, second = calibration_split(calibration_data["events"], calibration_data["y"])
    if reuse_estimator is not None:
        estimator = reuse_estimator
    else:
        estimator = (
            LogisticRegression(C=params["c"], max_iter=params["max_iter"], random_state=seed)
            if plugin == "lr"
            else RandomForestClassifier(**params, random_state=seed, n_jobs=1)
        )
        stage("fitting_train")
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            estimator.fit(train["x"], train["y"])
    stage(stage_name)
    raw = estimator.predict_proba(calibration_data["x"])[:, 1]
    calibrator = LogisticRegression(C=1e6, max_iter=200, random_state=seed)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        calibrator.fit(sigmoid_input(raw[first]), calibration_data["y"][first])
    calibrated = calibrator.predict_proba(sigmoid_input(raw[second]))[:, 1]
    threshold = threshold_for(calibration_data["y"][second], calibrated)
    return estimator, calibrator, threshold, calibration_partition, first, second


def load_reuse_estimator(directory, request):
    """加载服务层落好的池估计器，并核对复用契约与训练请求一致。"""
    reuse = request.get("reuse")
    if reuse is None:
        return None
    path = directory / "pool-estimator.skops"
    if not path.is_file():
        raise ModelError("MODEL_POOL_ARTIFACT_MISSING")
    estimator = sio.load(path, trusted=[])
    if sio.get_untrusted_types(file=path):
        raise ModelError("MODEL_POOL_ARTIFACT_UNTRUSTED")
    expected = pool.contract(
        request["plugin"], request["params"], request["features"], request["forms"]
    )
    if reuse.get("contract") != expected:
        raise ModelError("MODEL_POOL_CONTRACT_MISMATCH")
    return estimator


def run(directory, request, stage=lambda _: None):
    started = time.monotonic()
    # 验证集可选：validation.csv 不存在时跳过；训练与测试分区必须存在。
    partitions = {}
    for p in research.PARTITIONS:
        path = directory / (p + ".csv")
        if p == "validation" and not path.is_file():
            continue
        partitions[p] = load_partition(path)
    if "train" not in partitions or "test" not in partitions:
        raise ModelError("MODEL_DATA_LIMIT")
    features = request.get("features", list(research.NUMERIC_FIELDS))
    forms = request.get("forms") or {
        name: policy.DEFAULT_FORMS[request["plugin"]] for name in features
    }
    matrices, stats = feature_matrix(partitions, features, forms)
    for p, d in partitions.items():
        d["x"] = matrices[p]
    reuse_estimator = load_reuse_estimator(directory, request)
    model, calibrator, threshold, calibration_partition, first, second = fit_model(
        partitions,
        request["plugin"],
        request["params"],
        request["seed"],
        stage,
        reuse_estimator=reuse_estimator,
    )
    calibration_data = partitions[calibration_partition]
    threshold_key = (
        "validation_threshold" if calibration_partition == "validation" else "train_threshold"
    )
    bundle = {
        "model": model,
        "calibrator": calibrator,
        "threshold": threshold,
        "features": features,
        "forms": forms,
        "standardization": stats,
    }
    stage("evaluating_test")
    probabilities = {
        p: calibrator.predict_proba(sigmoid_input(model.predict_proba(d["x"])[:, 1]))[:, 1]
        for p, d in partitions.items()
    }
    model_path = directory / "model.skops"
    sio.dump(bundle, model_path)
    if sio.get_untrusted_types(file=model_path):
        raise ModelError("MODEL_ARTIFACT_UNTRUSTED")
    restored = sio.load(model_path, trusted=[])
    for p, d in partitions.items():
        roundtrip = restored["calibrator"].predict_proba(
            sigmoid_input(restored["model"].predict_proba(d["x"])[:, 1])
        )[:, 1]
        if not np.array_equal(roundtrip, probabilities[p]):
            raise ModelError("MODEL_PERSISTENCE_DRIFT")

    def score(p, mask=None):
        d = partitions[p]
        m = np.ones(len(d["y"]), dtype=bool) if mask is None else mask
        return metrics.evaluate(
            d["y"][m],
            probabilities[p][m],
            threshold,
            d["effort"][m],
            d["events"][m],
            d["identities"][m],
        )

    test = partitions["test"]
    prior = float(partitions["train"]["y"].mean())
    limitations = [
        "post_publication_weak_labels_not_gold",
        "not_historical_online_simulation",
        "negative_coverage_noise_language_overlap_unverified",
        (
            "no_validation_partition_train_internal_calibration"
            if calibration_partition == "train"
            else "validation_selected_metrics_not_unbiased"
        ),
        "no_candidate_release",
    ]
    if reuse_estimator is not None:
        limitations.append(
            "estimator_parameters_reused_from_pool_calibration_threshold_refit"
        )
    if request.get("reuse_auto_miss"):
        limitations.append("pool_auto_match_none_trained_from_scratch")
    report = {
        "provenance": provenance(),
        "request": request,
        "features": features,
        "forms": forms,
        "standardization": stats,
        "threshold": threshold,
        "calibration": {
            "method": f"{calibration_partition}-chronological-sigmoid-logit-v1",
            "calibration_rows": int(first.sum()),
            "threshold_rows": int(second.sum()),
            "boundary_epoch": int(calibration_data["events"][second][0]),
            "selection": "f1_grid_0.05_to_0.95_step_0.01_ties_higher",
            "calibration_members_hash": content_hash(
                calibration_data["identities"][first].tolist()
            ),
            "threshold_members_hash": content_hash(
                calibration_data["identities"][second].tolist()
            ),
        },
        "calibration_partition": calibration_partition,
        "metrics": {threshold_key: score(calibration_partition, second), "test": score("test")},
        "baseline": {
            "method": "constant_train_prior",
            "probability": prior,
            "metrics": metrics.evaluate(
                test["y"],
                np.full(len(test["y"]), prior),
                0.5,
                test["effort"],
                test["events"],
                test["identities"],
            ),
        },
        "intervals": metrics.intervals(
            test["y"],
            probabilities["test"],
            threshold,
            test["effort"],
            test["events"],
            test["identities"],
            request["seed"],
        ),
        "capabilities": {"research_binary": True, "candidate_release": False},
        "limitations": limitations,
        **(
            {"reused_from": request["reuse"]}
            if reuse_estimator is not None
            else {}
        ),
        "resources": {
            "elapsed_seconds": time.monotonic() - started,
            "threads": 1,
            "rss_bytes_at_completion": psutil.Process().memory_info().rss,
        },
    }
    assets.write_json(directory / "report.json", report)
    with (directory / "probabilities.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "partition",
                "canonical_repository",
                "commit_id",
                "event_epoch",
                "label",
                "probability",
            ]
        )
        for p, d in partitions.items():
            for row, probability in zip(d["rows"], probabilities[p], strict=True):
                writer.writerow([p, *(row[k] for k in research.CSV_FIELDS[:4]), float(probability)])
        stream.flush()
        os.fsync(stream.fileno())
    stage("artifacts_complete")
    return report


def main():
    import sys

    directory = assets.safe_path(Path(sys.argv[1]))
    pid, identity = int(sys.argv[2]), float(sys.argv[3])

    def watch_parent():
        while True:
            time.sleep(0.5)
            try:
                current = psutil.Process(pid)
                if current.create_time() != identity or current.status() == psutil.STATUS_ZOMBIE:
                    os._exit(70)
            except psutil.Error:
                os._exit(70)

    threading.Thread(target=watch_parent, daemon=True).start()
    request = assets.read_json(directory / "request.json", lambda: None)

    def stage(value):
        # Parent reads only this small trusted status; it is not publication evidence.
        (directory / "stage.txt").write_text(value, encoding="ascii")

    try:
        run(directory, request, stage)
    except (ModelError, ConvergenceWarning) as exc:
        (directory / "error.txt").write_text(
            exc.code if isinstance(exc, ModelError) else "MODEL_NOT_CONVERGED", encoding="ascii"
        )
        raise SystemExit(2) from None
    except Exception:
        (directory / "error.txt").write_text("MODEL_TRAINING_FAILED", encoding="ascii")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
