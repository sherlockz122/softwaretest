"""Predeclared same-population numerical, frozen semantic and fusion comparisons."""

import csv
import hashlib
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import numpy as np
import skops.io as sio
from threadpoolctl import threadpool_limits

from packages.mining import public_assets as assets
from packages.mining.research_dataset import NUMERIC_FIELDS, PARTITIONS
from packages.modeling import metrics
from packages.modeling.train import calibration_split, fit_model, sigmoid_input
from packages.semantic import artifacts
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

VARIANTS = ("numeric_lr", "numeric_rf", "semantic_lr", "fusion_lr")


def gain_gate(baseline, fused, seed):
    comparable = [
        baseline["pr_auc"],
        fused["pr_auc"],
        baseline["mcc"],
        fused["mcc"],
        baseline["brier"],
        fused["brier"],
        baseline["effort"]["pofb20"],
        fused["effort"]["pofb20"],
    ]
    if any(v is None or not np.isfinite(v) for v in comparable):
        return {"seed": seed, "ap_delta": None, "passed": False, "reason": "undefined_metric"}
    delta = fused["pr_auc"] - baseline["pr_auc"]
    return {
        "seed": seed,
        "ap_delta": delta,
        "passed": delta >= 0.02
        and fused["mcc"] >= baseline["mcc"]
        and fused["effort"]["pofb20"] >= baseline["effort"]["pofb20"]
        and fused["brier"] <= baseline["brier"],
    }


def partitions(rows, values, kind):
    if kind not in VARIANTS:
        raise SemanticError("SEMANTIC_VARIANT_INVALID")
    result = {}
    for part in PARTITIONS:
        selected = [i for i, r in enumerate(rows) if r["member"]["partition"] == part]
        members = [rows[i]["member"] for i in selected]
        if not members:
            raise SemanticError("SEMANTIC_CLASSES_OR_NUMBERS_UNUSABLE")
        numeric = np.array([[float(r[k]) for k in NUMERIC_FIELDS] for r in members], dtype=float)
        semantic = np.asarray(values[selected], dtype=float)
        x = (
            numeric
            if kind.startswith("numeric_")
            else semantic
            if kind == "semantic_lr"
            else np.concatenate([numeric, semantic], axis=1)
        )
        y = np.array([int(r["label"]) for r in members])
        if not len(members) or set(y) != {0, 1} or not np.isfinite(x).all() or (numeric < 0).any():
            raise SemanticError("SEMANTIC_CLASSES_OR_NUMBERS_UNUSABLE")
        events = np.array([int(r["event_epoch"]) for r in members], dtype=np.int64)
        if (np.diff(events) < 0).any():
            raise SemanticError("SEMANTIC_TIME_DRIFT")
        result[part] = {
            "x": x,
            "y": y,
            "events": events,
            "effort": numeric[:, 0] + numeric[:, 1],
            "identities": np.array(
                [r["canonical_repository"] + ":" + r["commit_id"] for r in members]
            ),
            "members": members,
        }
    calibration_split(result["validation"]["events"], result["validation"]["y"])
    return result


def probability(bundle, x):
    estimator, calibrator = bundle["model"], bundle["calibrator"]
    return calibrator.predict_proba(sigmoid_input(estimator.predict_proba(x)[:, 1]))[:, 1]


def fitted_hash(bundle):
    digest = hashlib.sha256()

    def array(value):
        a = np.asarray(value)
        if a.dtype.hasobject:
            raise SemanticError("SEMANTIC_STATE_INVALID")
        digest.update(str((a.dtype.str, a.shape)).encode())
        digest.update(a.tobytes(order="C"))

    estimator = bundle["model"]
    if hasattr(estimator, "steps"):
        scaler, model = estimator.steps[0][1], estimator.steps[1][1]
        for name in ["mean_", "scale_", "var_", "n_samples_seen_"]:
            array(getattr(scaler, name))
        for name in ["coef_", "intercept_", "classes_"]:
            array(getattr(model, name))
    else:
        array(estimator.classes_)
        for tree in estimator.estimators_:
            for name in [
                "children_left",
                "children_right",
                "feature",
                "threshold",
                "impurity",
                "n_node_samples",
                "weighted_n_node_samples",
                "value",
                "missing_go_to_left",
            ]:
                array(getattr(tree.tree_, name))
    for name in ["coef_", "intercept_", "classes_"]:
        array(getattr(bundle["calibrator"], name))
    array(bundle["threshold"])
    return digest.hexdigest()


def score(part, p, threshold):
    return metrics.evaluate(
        part["y"], p, threshold, part["effort"], part["events"], part["identities"]
    )


def train_round(parts, kind, seed):
    plugin = "rf" if kind == "numeric_rf" else "lr"
    params = (
        {"n_estimators": 100, "max_depth": 16, "min_samples_leaf": 2}
        if plugin == "rf"
        else {"c": 1.0, "max_iter": 500}
    )
    model, calibrator, threshold, first, second = fit_model(
        parts, plugin, params, seed, lambda _: checkpoint()
    )
    return (
        {
            "model": model,
            "calibrator": calibrator,
            "threshold": threshold,
            "kind": kind,
            "seed": seed,
        },
        first,
        second,
    )


def study(inputs, cache, output):
    inputs, cache, output = map(lambda p: assets.safe_path(Path(p)), (inputs, cache, output))
    original, encoded = artifacts.verify(inputs, checkpoint), artifacts.verify(cache, checkpoint)
    if encoded["binding"]["inputs"] != original["content_hash"] or encoded["shape"] != [
        original["accepted"],
        3076,
    ]:
        raise SemanticError("SEMANTIC_CACHE_BINDING_DRIFT")
    rows = list(artifacts.records(inputs))
    index = assets.read_json(cache / "rows.json", checkpoint, maximum=4 * 1024**2)["records"]
    if index != [{"member": r["member"], "text_hash": r["text_hash"]} for r in rows]:
        raise SemanticError("SEMANTIC_CACHE_MEMBER_DRIFT")
    values = np.load(cache / "embeddings.npy", allow_pickle=False, mmap_mode="r")
    if (
        values.dtype != np.float32
        or list(values.shape) != encoded["shape"]
        or not np.isfinite(values).all()
    ):
        raise SemanticError("SEMANTIC_CACHE_SHAPE_DRIFT")
    output.mkdir(parents=True, exist_ok=True)
    attempt = assets.safe_path(output / (".study-" + uuid4().hex))
    attempt.mkdir()
    reports, files, initial = [], [], {}
    plan = [(k, s, False) for s in [42, 17] for k in VARIANTS] + [(k, 42, True) for k in VARIANTS]
    with threadpool_limits(limits=1):
        for kind, seed, repeated in plan:
            checkpoint()
            started = time.monotonic()
            parts = partitions(rows, values, kind)
            bundle, first, second = train_round(parts, kind, seed)
            name = f"{kind}-{seed}" + ("-repeat" if repeated else "")
            model_name = name + ".skops"
            prob_name = name + ".csv"
            sio.dump(bundle, attempt / model_name)
            if sio.get_untrusted_types(file=attempt / model_name):
                raise SemanticError("SEMANTIC_ARTIFACT_UNTRUSTED")
            restored = sio.load(attempt / model_name, trusted=[])
            probabilities = {part: probability(bundle, parts[part]["x"]) for part in PARTITIONS}
            if any(
                not np.array_equal(p, probability(restored, parts[part]["x"]))
                for part, p in probabilities.items()
            ):
                raise SemanticError("SEMANTIC_PERSISTENCE_DRIFT")
            with (attempt / prob_name).open("x", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "partition",
                        "canonical_repository",
                        "commit_id",
                        "label",
                        "probability",
                        "predicted",
                    ]
                )
                for part in PARTITIONS:
                    for row, p in zip(parts[part]["members"], probabilities[part], strict=True):
                        writer.writerow(
                            [
                                part,
                                row["canonical_repository"],
                                row["commit_id"],
                                row["label"],
                                format(float(p), ".17g"),
                                int(p >= bundle["threshold"]),
                            ]
                        )
            record = {
                "kind": kind,
                "seed": seed,
                "repeated": repeated,
                "threshold": bundle["threshold"],
                "numeric_state_sha256": fitted_hash(bundle),
                "probabilities": artifacts.file_digest(attempt / prob_name),
                "metrics": {
                    part: score(parts[part], p, bundle["threshold"])
                    for part, p in probabilities.items()
                },
                "calibration_rows": int(first.sum()),
                "threshold_rows": int(second.sum()),
                "fit_and_evaluation_seconds": time.monotonic() - started,
            }
            test = parts["test"]
            p = probabilities["test"]
            test_rows = [r for r in rows if r["member"]["partition"] == "test"]
            groups = np.array(
                [
                    "Java_extension_only"
                    if r["files"] and all(f["language_hint"] == "Java" for f in r["files"])
                    else "mixed_or_unknown"
                    for r in test_rows
                ]
            )
            record["language_hint_results"] = {}
            for group in sorted(set(groups)):
                mask = groups == group
                record["language_hint_results"][group] = metrics.evaluate(
                    test["y"][mask],
                    p[mask],
                    bundle["threshold"],
                    test["effort"][mask],
                    test["events"][mask],
                    test["identities"][mask],
                )
            record["test_intervals"] = metrics.intervals(
                test["y"],
                p,
                bundle["threshold"],
                test["effort"],
                test["events"],
                test["identities"],
                seed,
            )
            prior = float(parts["train"]["y"].mean())
            record["constant_train_prior_baseline"] = score(test, np.full(len(p), prior), 0.5)
            # Diagnostics use the already trained model/cache, never refit or choose settings.
            if kind in {"semantic_lr", "fusion_lr"}:
                offset = 0 if kind == "semantic_lr" else len(NUMERIC_FIELDS)
                swapped = test["x"].copy()
                muted = test["x"].copy()
                a, b = slice(offset + 768, offset + 1536), slice(offset + 1536, offset + 2304)
                swapped[:, a], swapped[:, b] = test["x"][:, b], test["x"][:, a]
                swapped[:, [offset + 3073, offset + 3074]] = test["x"][
                    :, [offset + 3074, offset + 3073]
                ]
                muted[:, offset : offset + 768] = 0
                muted[:, offset + 3072] = 0
                record["counterfactual_diagnostics"] = {
                    label: {
                        "mean_absolute_probability_change": float(
                            np.abs(probability(bundle, x) - p).mean()
                        ),
                        "scope": "input shortcut sensitivity only, not causal explanation",
                    }
                    for label, x in [("swap_added_deleted", swapped), ("mask_message", muted)]
                }
            if repeated:
                old = initial[kind]
                if (
                    old["numeric_state_sha256"] != record["numeric_state_sha256"]
                    or old["probabilities"] != record["probabilities"]
                ):
                    raise SemanticError("SEMANTIC_MODEL_REPRODUCTION_DRIFT")
            elif seed == 42:
                initial[kind] = record
            reports.append(record)
            files.extend([model_name, prob_name])
            print(
                __import__("json").dumps(
                    {
                        "kind": kind,
                        "seed": seed,
                        "repeat": repeated,
                        "test_ap": record["metrics"]["test"]["pr_auc"],
                    }
                ),
                flush=True,
            )
    gates = []
    for seed in [42, 17]:
        baseline = next(
            r
            for r in reports
            if r["kind"] == "numeric_lr" and r["seed"] == seed and not r["repeated"]
        )["metrics"]["test"]
        fused = next(
            r
            for r in reports
            if r["kind"] == "fusion_lr" and r["seed"] == seed and not r["repeated"]
        )["metrics"]["test"]
        gates.append(gain_gate(baseline, fused, seed))
    report = {
        "version": "same-population-semantic-pilot-v1",
        "runs": reports,
        "predeclared_gain_gates": gates,
        "observed_gain_gate_met_both_seeds": all(g["passed"] for g in gates),
        "models_selected_using_test": False,
        "candidate_release": False,
        "raw_serializer_byte_reproducibility_certified": False,
        "limits": [
            "small single-project test; not statistical or cross-language proof",
            "post-publication weak labels; historical availability not certified",
            "CodeSearchNet pretraining overlap not excluded",
            "file extension is only a language hint; unknown/mixed coverage retained",
        ],
    }
    assets.write_json(attempt / "report.json", report)
    files.append("report.json")
    if (
        artifacts.verify(inputs, checkpoint) != original
        or artifacts.verify(cache, checkpoint) != encoded
    ):
        raise SemanticError("SEMANTIC_INPUT_DRIFT")
    metadata = {
        "version": report["version"],
        "inputs_manifest": original["content_hash"],
        "cache_manifest": encoded["content_hash"],
        "runs": len(reports),
        "fitted_repeat_exact": True,
        "sources": artifacts.sources(
            [
                "packages/semantic/study.py",
                "packages/semantic/policy.py",
                "packages/semantic/artifacts.py",
                "packages/semantic/control.py",
                "scripts/semantic_pilot.py",
                "packages/modeling/train.py",
                "packages/modeling/metrics.py",
                "tools/semantic/uv.lock",
            ]
        ),
        "dependencies": {
            p: version(p)
            for p in [
                "scikit-learn",
                "numpy",
                "scipy",
                "skops",
                "psutil",
                "joblib",
                "threadpoolctl",
            ]
        },
        "research_only": True,
        "production_models": [],
    }
    checkpoint()
    return artifacts.publish(attempt, output, metadata, files)
