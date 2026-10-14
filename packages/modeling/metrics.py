"""Deterministic metrics with explicit undefined cases and whole-commit effort."""

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    matthews_corrcoef,
    precision_score,
    recall_score,
    roc_auc_score,
)


def effort_metrics(y, p, effort, events, identities):
    y, p, effort = np.asarray(y), np.asarray(p), np.asarray(effort, dtype=float)
    if not y.sum() or effort.sum() <= 0:
        return {"pofb20": None, "ifa": None, "popt": None, "reason": "no_buggy_or_no_effort"}
    keys = np.asarray(identities)
    # SHA precedes repository when score/time tie, as in the canonical metric contract.
    shas = np.asarray([str(k).rsplit(":", 1)[-1] for k in keys])
    order = np.lexsort((keys, shas, events, -p))
    found = np.flatnonzero(y[order])
    boundary = int(np.searchsorted(np.cumsum(effort[order]), effort.sum() * 0.2))

    def area(indices):
        x = np.r_[0, np.cumsum(effort[indices]) / effort.sum()]
        z = np.r_[0, np.cumsum(y[indices]) / y.sum()]
        return float(np.trapezoid(z, x))

    density = np.divide(y, effort, out=np.zeros(len(y), dtype=float), where=effort > 0)
    density[(y == 1) & (effort == 0)] = np.inf
    best = np.lexsort((keys, shas, events, -density))
    worst = np.lexsort((keys, shas, events, density))
    upper, lower = area(best), area(worst)
    return {
        "pofb20": float(y[order[: boundary + 1]].sum() / y.sum()),
        "ifa": int(found[0]),
        "popt": (area(order) - lower) / (upper - lower) if upper > lower else None,
        "reason": None if upper > lower else "equal_reference_areas",
    }


def evaluate(y, p, threshold, effort, events, identities):
    y, p = np.asarray(y), np.asarray(p)
    predicted = p >= threshold
    both = len(np.unique(y)) == 2
    reliability = []
    for index in range(10):
        included = (p >= index / 10) & (p < (index + 1) / 10 if index < 9 else p <= 1)
        reliability.append(
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "count": int(included.sum()),
                "probability": float(p[included].mean()) if included.any() else None,
                "observed_buggy": float(y[included].mean()) if included.any() else None,
            }
        )
    return {
        "support": {"clean": int((y == 0).sum()), "buggy": int(y.sum())},
        "precision": float(precision_score(y, predicted, zero_division=0)),
        "recall": float(recall_score(y, predicted, zero_division=0)),
        "f1": float(f1_score(y, predicted, zero_division=0)),
        "mcc": float(matthews_corrcoef(y, predicted)) if both else None,
        "roc_auc": float(roc_auc_score(y, p)) if both else None,
        "pr_auc": float(average_precision_score(y, p)) if both else None,
        "pr_auc_definition": "average_precision",
        "undefined_reason": None if both else "single_class",
        "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
        "brier": float(brier_score_loss(y, p)),
        "reliability": reliability,
        "effort": effort_metrics(y, p, effort, events, identities),
    }


def intervals(y, p, threshold, effort, events, identities, seed, repeats=100):
    # Resample contiguous groups, never individual adjacent commits independently.
    groups = np.array_split(np.unique(events), min(20, len(np.unique(events))))
    blocks = [np.flatnonzero(np.isin(events, group)) for group in groups]
    rng, samples = (
        np.random.default_rng(seed),
        {k: [] for k in ("pr_auc", "mcc", "brier", "pofb20")},
    )
    for _ in range(repeats):
        chosen = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
        if len(np.unique(y[chosen])) != 2:
            continue
        yt, pt = y[chosen], p[chosen]
        samples["pr_auc"].append(float(average_precision_score(yt, pt)))
        samples["mcc"].append(float(matthews_corrcoef(yt, pt >= threshold)))
        samples["brier"].append(float(brier_score_loss(yt, pt)))
        value = effort_metrics(yt, pt, effort[chosen], events[chosen], identities[chosen])["pofb20"]
        if value is not None:
            samples["pofb20"].append(value)
    return {
        "method": "contiguous-time-group-block-bootstrap",
        "repeats": repeats,
        "seed": seed,
        "interpretation": "descriptive_conditional_not_label_validity",
        "metrics": {
            k: {
                "valid": len(v),
                "lower": float(np.quantile(v, 0.025)) if v else None,
                "upper": float(np.quantile(v, 0.975)) if v else None,
            }
            for k, v in samples.items()
        },
    }
