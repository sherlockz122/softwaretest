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
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.source_evidence import content_hash
from packages.modeling import metrics
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


def fit_model(partitions, plugin, params, seed, stage=lambda _: None):
    train, validation = partitions["train"], partitions["validation"]
    first, second = calibration_split(validation["events"], validation["y"])
    estimator = (
        make_pipeline(
            StandardScaler(),
            LogisticRegression(C=params["c"], max_iter=params["max_iter"], random_state=seed),
        )
        if plugin == "lr"
        else RandomForestClassifier(**params, random_state=seed, n_jobs=1)
    )
    stage("fitting_train")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        estimator.fit(train["x"], train["y"])
    stage("calibrating_validation")
    raw = estimator.predict_proba(validation["x"])[:, 1]
    calibrator = LogisticRegression(C=1e6, max_iter=200, random_state=seed)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        calibrator.fit(sigmoid_input(raw[first]), validation["y"][first])
    calibrated = calibrator.predict_proba(sigmoid_input(raw[second]))[:, 1]
    threshold = threshold_for(validation["y"][second], calibrated)
    return estimator, calibrator, threshold, first, second


def run(directory, request, stage=lambda _: None):
    started = time.monotonic()
    partitions = {p: load_partition(directory / (p + ".csv")) for p in research.PARTITIONS}
    model, calibrator, threshold, first, second = fit_model(
        partitions, request["plugin"], request["params"], request["seed"], stage
    )
    bundle = {
        "model": model,
        "calibrator": calibrator,
        "threshold": threshold,
        "features": list(research.NUMERIC_FIELDS),
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
    report = {
        "provenance": provenance(),
        "request": request,
        "threshold": threshold,
        "calibration": {
            "method": "validation-chronological-sigmoid-logit-v1",
            "calibration_rows": int(first.sum()),
            "threshold_rows": int(second.sum()),
            "boundary_epoch": int(partitions["validation"]["events"][second][0]),
            "selection": "f1_grid_0.05_to_0.95_step_0.01_ties_higher",
            "calibration_members_hash": content_hash(
                partitions["validation"]["identities"][first].tolist()
            ),
            "threshold_members_hash": content_hash(
                partitions["validation"]["identities"][second].tolist()
            ),
        },
        "metrics": {"validation_threshold": score("validation", second), "test": score("test")},
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
        "limitations": [
            "post_publication_weak_labels_not_gold",
            "not_historical_online_simulation",
            "negative_coverage_noise_language_overlap_unverified",
            "validation_selected_metrics_not_unbiased",
            "no_candidate_release",
        ],
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
