"""Explicit counterfactual horizon labels, independent of actual production observations."""

import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from packages.mining.causal_features import VERSION as FEATURE_VERSION
from packages.mining.causal_features import CausalRun
from packages.mining.features import FEATURES, SHA, digest, utc

VERSION = "historical-horizon-replay-v1"


class ReplayError(ValueError):
    pass


def hashed(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True)
class ReplayPolicy:
    as_of: datetime
    horizon_days: int = 90
    knowledge_lag_days: int = 7

    def __post_init__(self):
        utc(self.as_of)
        if (
            type(self.horizon_days) is not int
            or not 1 <= self.horizon_days <= 3650
            or type(self.knowledge_lag_days) is not int
            or not 0 <= self.knowledge_lag_days <= 3650
        ):
            raise ReplayError("REPLAY_POLICY_INVALID")


@dataclass(frozen=True)
class ReplaySource:
    repository: str
    head_sha: str
    acquired_at: datetime
    manifest_digest: str
    evidence_digest: str
    coverage_start: datetime
    coverage_end: datetime
    coverage_complete: bool
    timezone_verified: bool
    label_kind: str = "szz_silver"

    def __post_init__(self):
        for value in (self.acquired_at, self.coverage_start, self.coverage_end):
            utc(value)
        if (
            not isinstance(self.repository, str)
            or not self.repository
            or len(self.repository) > 128
            or not isinstance(self.head_sha, str)
            or not SHA.fullmatch(self.head_sha)
            or not hashed(self.manifest_digest)
            or not hashed(self.evidence_digest)
            or self.coverage_start > self.coverage_end
            or self.coverage_end > self.acquired_at
            or type(self.coverage_complete) is not bool
            or type(self.timezone_verified) is not bool
            or self.label_kind not in ("szz_silver", "reviewed_gold")
        ):
            raise ReplayError("REPLAY_SOURCE_INVALID")


@dataclass(frozen=True)
class FixLink:
    fix_sha: str
    fix_time: datetime
    ancestry_verified: bool

    def __post_init__(self):
        utc(self.fix_time)
        if (
            not isinstance(self.fix_sha, str)
            or not SHA.fullmatch(self.fix_sha)
            or type(self.ancestry_verified) is not bool
        ):
            raise ReplayError("REPLAY_LINK_INVALID")


@dataclass(frozen=True)
class ReplayOutcome:
    sha: str
    links: tuple[FixLink, ...] = ()
    unresolved: bool = False

    def __post_init__(self):
        if (
            not isinstance(self.sha, str)
            or not SHA.fullmatch(self.sha)
            or type(self.links) is not tuple
            or any(not isinstance(link, FixLink) for link in self.links)
            or len({link.fix_sha for link in self.links}) != len(self.links)
            or type(self.unresolved) is not bool
        ):
            raise ReplayError("REPLAY_OUTCOME_INVALID")


@dataclass(frozen=True)
class ReplayAudit:
    canonical_json: str

    def as_dict(self):
        return json.loads(self.canonical_json)

    @property
    def content_hash(self):
        return digest(self.as_dict())


def audit_replay(run, outcomes, source, policy, *, seconds=30, tick=lambda: None):
    """Caller certifies source completeness/ancestry; no published CSV can imply them."""
    if (
        not isinstance(run, CausalRun)
        or not isinstance(source, ReplaySource)
        or not isinstance(policy, ReplayPolicy)
        or type(outcomes) is not tuple
        or any(not isinstance(row, ReplayOutcome) for row in outcomes)
        or type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 30
        or source.coverage_end > policy.as_of
        or policy.as_of > source.acquired_at
    ):
        raise ReplayError("REPLAY_INPUT_INVALID")
    if (
        len(run.canonical_json.encode()) > 32 * 1024**2
        or len(outcomes) > 1000
        or sum(len(row.links) for row in outcomes) > 10000
    ):
        raise ReplayError("REPLAY_RESOURCE_LIMIT")
    started = time.perf_counter()

    def check():
        tick()
        if time.perf_counter() - started >= seconds:
            raise ReplayError("REPLAY_TIMEOUT")

    try:
        data = run.as_dict()
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise ReplayError("REPLAY_FEATURE_INVALID") from None
    vectors = data.get("vectors", [])
    if data.get("feature_version") != FEATURE_VERSION or not hashed(data.get("rule_hash")):
        raise ReplayError("REPLAY_FEATURE_VERSION")
    if not isinstance(vectors, list) or not 1 <= len(vectors) <= 1000:
        raise ReplayError("REPLAY_RESOURCE_LIMIT")
    if data.get("repository") != source.repository or data.get("head_sha") != source.head_sha:
        raise ReplayError("REPLAY_SOURCE_CONFLICT")
    if any(not isinstance(vector, dict) for vector in vectors):
        raise ReplayError("REPLAY_VECTOR_INVALID")
    if any(not isinstance(v.get("sha"), str) or not SHA.fullmatch(v["sha"]) for v in vectors):
        raise ReplayError("REPLAY_VECTOR_INVALID")
    known = {row.sha: row for row in outcomes}
    if len(known) != len(outcomes) or len({v.get("sha") for v in vectors}) != len(vectors):
        raise ReplayError("REPLAY_DUPLICATE_SHA")
    if set(known) - {v.get("sha") for v in vectors} or source.head_sha not in {
        v.get("sha") for v in vectors
    }:
        raise ReplayError("REPLAY_POPULATION_INVALID")
    rows = []
    for vector in vectors:
        check()
        try:
            event = datetime.fromisoformat(vector["event_time"].removesuffix("Z"))
            utc(event)
            values, reasons = vector["values"], vector["reasons"]
            if (
                not SHA.fullmatch(vector["sha"])
                or vector["feature_version"] != FEATURE_VERSION
                or not hashed(vector["source_digest"])
                or set(values) != set(FEATURES)
                or set(reasons) - set(FEATURES)
                or any(
                    value is not None
                    and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)
                    for value in values.values()
                )
                or values["FIX"] not in (None, 0, 1)
                or any(values[key] is None and not reasons.get(key) for key in FEATURES)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ReplayError("REPLAY_VECTOR_INVALID") from None
        end = event + timedelta(days=policy.horizon_days)
        outcome = known.get(vector["sha"])
        label, reason, available = "Unknown", "outcome_unavailable", None
        if event > policy.as_of:
            reason = "future_event"
        elif not source.timezone_verified:
            reason = "clock_unverified"
        elif outcome is not None:
            links = sorted(outcome.links, key=lambda link: (link.fix_time, link.fix_sha))
            if outcome.unresolved:
                reason = "unresolved_evidence"
            elif any(
                not link.ancestry_verified
                or link.fix_sha == outcome.sha
                or link.fix_time < event
                or link.fix_time > source.coverage_end
                for link in links
            ):
                reason = "link_unverified_or_inconsistent"
            elif within := [link for link in links if link.fix_time <= end]:
                available = within[0].fix_time + timedelta(days=policy.knowledge_lag_days)
                if available <= policy.as_of:
                    label, reason = "buggy", "linked_fix_within_horizon"
                else:
                    reason = "positive_not_yet_known"
            elif (
                source.coverage_complete
                and source.coverage_start <= event
                and end <= source.coverage_end
            ):
                available = end + timedelta(days=policy.knowledge_lag_days)
                if available <= policy.as_of:
                    label, reason = "clean", "no_observed_link_within_horizon"
                else:
                    reason = "negative_not_yet_known"
            else:
                reason = "negative_censored_or_incomplete"
        rows.append(
            {
                "sha": vector["sha"],
                "event_time": event,
                "horizon_end": end,
                "label": label,
                "label_reason": reason,
                "reconstructed_available_at": available,
                "values": values,
                "reasons": reasons,
                "label_kind": source.label_kind,
            }
        )
    if len({v["source_digest"] for v in vectors}) != 1:
        raise ReplayError("REPLAY_MIXED_FEATURE_RUNS")
    rows.sort(key=lambda row: (row["event_time"], row["sha"]))
    groups = []
    for row in rows:
        if not groups or groups[-1][0]["event_time"] != row["event_time"]:
            groups.append([])
        groups[-1].append(row)
    splits, deficits, purged = {}, [], []
    if len(groups) < 3:
        deficits.append("insufficient_time_groups")
    else:
        cumulative, count = [], 0
        for group in groups:
            count += len(group)
            cumulative.append(count)
        first = min(
            next(i + 1 for i, n in enumerate(cumulative) if n >= count * 0.7), len(groups) - 2
        )
        second = min(
            max(next(i + 1 for i, n in enumerate(cumulative) if n >= count * 0.85), first + 1),
            len(groups) - 1,
        )
        selections = (groups[:first], groups[first:second], groups[second:])
        cuts = (groups[first][0]["event_time"], groups[second][0]["event_time"], policy.as_of)
        for name, selected, cutoff in zip(
            ("train", "validation", "test"), selections, cuts, strict=True
        ):
            check()
            eligible = []
            population = [row for group in selected for row in group]
            for row in population:
                if row["label"] == "Unknown":
                    continue
                available = row["reconstructed_available_at"]
                if available >= cutoff if name != "test" else available > cutoff:
                    purged.append(
                        {"sha": row["sha"], "split": name, "reason": "label_after_cutoff"}
                    )
                else:
                    eligible.append(row)
            classes = Counter(row["label"] for row in eligible)
            if set(classes) != {"buggy", "clean"}:
                deficits.append(name + "_missing_class")
            splits[name] = {
                "boundary_start": population[0]["event_time"],
                "boundary_end": population[-1]["event_time"],
                "knowledge_cutoff": cutoff,
                "membership": [row["sha"] for row in population],
                "eligible": [row["sha"] for row in eligible],
                "classes": dict(sorted(classes.items())),
            }
            if name == "train":
                if any(
                    not any(row["values"][key] is not None for row in eligible) for key in FEATURES
                ):
                    deficits.append("train_all_missing_features")
                splits[name]["class_weights"] = {
                    key: len(eligible) / (2 * number) for key, number in sorted(classes.items())
                }
    canonical = {
        "version": VERSION,
        "feature_version": FEATURE_VERSION,
        "scope": "counterfactual replay of observed horizon links, not actual production knowledge",
        "source": asdict(source),
        "policy": asdict(policy),
        "feature_content_hash": run.content_hash,
        "evidence": [
            asdict(replace(row, links=tuple(sorted(row.links, key=lambda link: link.fix_sha))))
            for row in sorted(outcomes, key=lambda row: row.sha)
        ],
        "rule_hash": digest(Path(__file__).read_text(encoding="utf-8")),
        "rows": rows,
        "labels": dict(sorted(Counter(row["label"] for row in rows).items())),
        "splits": splits,
        "purged": purged,
        "deficits": deficits,
        "replay_structurally_ready": not deficits,
        "product_training_ready": False,
        "preprocessing": {"fit_population": "train only", "fitted": False},
        "evaluation": (
            "later labels up to as_of; observed fixed-horizon endpoint, silver/gold stated"
        ),
    }

    # digest encodes naive UTC with Z; preserve canonical value semantics.
    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat(timespec="microseconds") + "Z"
        raise TypeError

    check()
    return ReplayAudit(json.dumps(canonical, default=encode, sort_keys=True, separators=(",", ":")))
