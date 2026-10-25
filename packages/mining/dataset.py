"""Immutable maturity and two-time split kernel; no fitted transforms or database IO."""

import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from packages.mining.features import (
    FEATURE_VERSION,
    FEATURES,
    RULE_HASH,
    SHA,
    FeatureVector,
    Snapshot,
    digest,
    utc,
)
from packages.repositories.parse_policy import PARSER_VERSION

LABEL_VERSION = "time-labels-v1"

# 标签观察窗口的默认天数。探针实测后由 90 上调至 180：90 天在 ActiveMQ 上只覆盖 35.2%
# 的真实缺陷引入提交，其余会被标成 clean；180 天把覆盖率提到 43.4%，右端只多丢 1.9%。
# 这只是无法按仓库标定时的回退值，标定结果写入数据集策略快照并优先于它。
WINDOW_DAYS = 180


class DatasetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Policy:
    snapshot_at: datetime
    window_days: int = WINDOW_DAYS
    train: float = 0.70
    validation: float = 0.15
    test: float = 0.15

    def __post_init__(self):
        utc(self.snapshot_at)
        if type(self.window_days) is not int or not 1 <= self.window_days <= 3650:
            raise DatasetError("DATA_WINDOW_INVALID")
        ratios = (self.train, self.validation, self.test)
        if any(
            type(x) not in (int, float) or not math.isfinite(x) or not 0 < x < 1 for x in ratios
        ):
            raise DatasetError("DATA_SPLIT_INVALID")
        if not math.isclose(sum(ratios), 1, abs_tol=1e-12):
            raise DatasetError("DATA_SPLIT_INVALID")


@dataclass(frozen=True)
class Link:
    blamed_sha: str
    eligible: bool

    def __post_init__(self):
        if (
            not isinstance(self.blamed_sha, str)
            or not SHA.fullmatch(self.blamed_sha)
            or type(self.eligible) is not bool
        ):
            raise DatasetError("DATA_LINK_INVALID")


@dataclass(frozen=True)
class Evidence:
    sha: str
    available_at: datetime
    candidate: bool
    review_status: str
    review_revision: int
    current_review_matches: bool
    content_complete: bool
    issues_complete: bool
    trace_status: str
    links: tuple[Link, ...] = ()

    def __post_init__(self):
        utc(self.available_at)
        if (
            type(self.links) is not tuple
            or any(not isinstance(x, Link) for x in self.links)
            or self.review_status not in ("unreviewed", "confirmed", "rejected")
            or type(self.review_revision) is not int
            or self.review_revision < 0
            or any(
                type(x) is not bool
                for x in (
                    self.candidate,
                    self.current_review_matches,
                    self.content_complete,
                    self.issues_complete,
                )
            )
            or self.review_status == "rejected"
            and self.candidate
            or self.review_status == "confirmed"
            and not self.candidate
        ):
            raise DatasetError("DATA_EVIDENCE_INVALID")


@dataclass(frozen=True)
class Observation:
    run_id: str
    head_sha: str
    observed_at: datetime
    published_at: datetime
    as_of: datetime
    source_digest: str
    records: tuple[Evidence, ...]
    history_coverage: str = "full"
    parser_version: str = PARSER_VERSION

    def __post_init__(self):
        for value in (self.observed_at, self.published_at, self.as_of):
            utc(value)
        if (
            not self.run_id
            or not self.source_digest
            or self.observed_at > self.published_at
            or self.history_coverage not in ("full", "recent_window")
            or self.parser_version != PARSER_VERSION
            or type(self.records) is not tuple
            or any(not isinstance(e, Evidence) for e in self.records)
            or len({e.sha for e in self.records}) != len(self.records)
        ):
            raise DatasetError("DATA_OBSERVATION_INVALID")


@dataclass(frozen=True)
class FrozenDataset:
    """Canonical JSON has value semantics; accessors return independent copies."""

    canonical_json: str

    def as_dict(self):
        import json

        return json.loads(self.canonical_json)

    @property
    def content_hash(self):
        return digest(self.as_dict())


def rule_hash():
    # Bind source implementation, not only the public numeric label name.
    return digest(
        [
            Path(__file__).with_name(p).read_text(encoding="utf-8")
            for p in ("dataset.py", "dataset_source.py")
        ]
    )


def audit(snapshot: Snapshot, vectors, observations, policy: Policy, tick=lambda: None):
    """Unknown/purge stay in the audit; no raw value is imputed, scaled or sampled."""
    if snapshot.history_coverage != "full":
        raise DatasetError("DATA_FEATURE_COVERAGE_INCOMPLETE")
    if type(vectors) is not tuple or any(not isinstance(v, FeatureVector) for v in vectors):
        raise DatasetError("DATA_FROZEN_VECTORS_REQUIRED")
    if type(observations) is not tuple or any(not isinstance(o, Observation) for o in observations):
        raise DatasetError("DATA_FROZEN_OBSERVATIONS_REQUIRED")
    index = {c.sha: c for c in snapshot.commits}
    values = {v.sha: v for v in vectors}
    if len(values) != len(vectors) or set(values) != set(index):
        raise DatasetError("DATA_FEATURE_POPULATION_INVALID")
    if len({v.source_digest for v in vectors}) > 1 or any(
        v.event_time != index[v.sha].event_time for v in vectors
    ):
        raise DatasetError("DATA_FEATURE_SOURCE_CONFLICT")
    if len({o.run_id for o in observations}) != len(observations):
        raise DatasetError("DATA_DUPLICATE_OBSERVATION")
    ordered = sorted(snapshot.commits, key=lambda c: c.order)

    def ancestors(sha):
        visited, stack = set(), [sha]
        while stack:
            tick()
            current = stack.pop()
            if current not in visited:
                if current not in index:
                    raise DatasetError("DATA_OBSERVATION_OUTSIDE_HEAD")
                visited.add(current)
                stack.extend(index[current].parents)
        return visited

    rounds, positives, conflicts, unresolved = [], {}, set(), set()
    for observation in sorted(observations, key=lambda o: o.run_id):
        tick()
        reachable = ancestors(observation.head_sha)
        if set(e.sha for e in observation.records) != reachable:
            raise DatasetError("DATA_OBSERVATION_POPULATION_INVALID")
        blocked = set()
        available = max([observation.published_at, *(e.available_at for e in observation.records)])
        for evidence in observation.records:
            tick()
            commit = index[evidence.sha]
            lineage = None
            invalid = (
                not evidence.current_review_matches
                or not evidence.content_complete
                or not evidence.issues_complete
                or commit.event_time > observation.observed_at
                or evidence.available_at > observation.as_of
                or evidence.candidate
                and (
                    evidence.review_status != "confirmed"
                    or evidence.trace_status not in ("traced", "no_traceable_lines")
                )
            )
            if invalid:
                lineage = ancestors(evidence.sha)
                blocked.update(lineage)
                if (
                    observation.published_at <= policy.snapshot_at
                    and evidence.available_at <= policy.snapshot_at
                ):
                    unresolved.update(lineage)
            if not evidence.current_review_matches:
                conflicts.update(link.blamed_sha for link in evidence.links)
            if invalid or not evidence.candidate or observation.history_coverage != "full":
                continue
            if available > policy.snapshot_at:
                continue
            for link in evidence.links:
                lineage = lineage or ancestors(evidence.sha)
                if (
                    type(link.eligible) is not bool
                    or not link.eligible
                    or link.blamed_sha == evidence.sha
                    or link.blamed_sha not in lineage
                    or index[link.blamed_sha].event_time > commit.event_time
                ):
                    raise DatasetError("DATA_LINK_INVALID")
                positives[link.blamed_sha] = min(
                    positives.get(link.blamed_sha, available), available
                )
        rounds.append((observation, reachable, blocked, available))

    rows = []
    for commit in ordered:
        tick()
        vector = values[commit.sha]
        reason, label, known_at = None, "unknown", None
        if commit.event_time > policy.snapshot_at:
            reason = "event_after_snapshot"
        elif commit.sha in conflicts:
            reason = "review_conflict"
        elif (
            commit.parse_status != "parsed"
            or len(commit.parents) > 1
            or any(not f.supported for f in commit.files)
        ):
            reason = "content_incomplete"
        elif commit.sha in positives:
            label, known_at = "buggy", positives[commit.sha]
        elif commit.sha in unresolved:
            reason = "unresolved_evidence"
        elif commit.event_time + timedelta(days=policy.window_days) > policy.snapshot_at:
            reason = "immature"
        else:
            proofs = [
                max(available, commit.event_time + timedelta(days=policy.window_days))
                for o, reachable, blocked, available in rounds
                if o.history_coverage == "full"
                and commit.sha in reachable
                and commit.sha not in blocked
                and min(o.observed_at, o.as_of)
                >= commit.event_time + timedelta(days=policy.window_days)
                and available <= policy.snapshot_at
            ]
            if proofs:
                label, known_at = "clean", min(proofs)
            else:
                reason = "observation_incomplete_or_unresolved"
        rows.append(
            {
                "sha": commit.sha,
                "event_time": commit.event_time,
                "label": label,
                "label_available_at": known_at,
                "reason": reason,
                "values": dict(vector.values),
                "missing_reasons": dict(vector.reasons),
            }
        )

    eligible = [r for r in rows if r["label"] != "unknown"]
    groups = []
    for row in eligible:
        if not groups or groups[-1][0]["event_time"] != row["event_time"]:
            groups.append([])
        groups[-1].append(row)
    deficits, boundaries = [], {}
    partitions = {name: [] for name in ("train", "validation", "test")}
    purged = []
    if len(groups) < 3:
        deficits.append("insufficient_time_groups")
    else:
        # Select boundaries from the original population; never move them after purge.
        train_target = len(eligible) * policy.train
        validation_target = len(eligible) * (policy.train + policy.validation)
        cumulative = []
        count = 0
        for group in groups:
            count += len(group)
            cumulative.append(count)
        first = min(
            max(next(i + 1 for i, n in enumerate(cumulative) if n >= train_target), 1),
            len(groups) - 2,
        )
        second = min(
            max(next(i + 1 for i, n in enumerate(cumulative) if n >= validation_target), first + 1),
            len(groups) - 1,
        )
        validation_start, test_start = (
            groups[first][0]["event_time"],
            groups[second][0]["event_time"],
        )
        boundaries = {"validation_start": validation_start, "test_start": test_start}
        for i, group in enumerate(groups):
            name = "train" if i < first else "validation" if i < second else "test"
            cutoff = (
                validation_start
                if name == "train"
                else test_start
                if name == "validation"
                else policy.snapshot_at
            )
            for row in group:
                if (
                    row["label_available_at"] >= cutoff
                    if name != "test"
                    else row["label_available_at"] > cutoff
                ):
                    purged.append(
                        {"sha": row["sha"], "split": name, "reason": "label_after_cutoff"}
                    )
                else:
                    partitions[name].append(row["sha"])
        for name, shas in partitions.items():
            chosen = set(shas)
            if {r["label"] for r in eligible if r["sha"] in chosen} != {"buggy", "clean"}:
                deficits.append(name + "_missing_class")

    train_shas = set(partitions["train"])
    selected_train = [r for r in eligible if r["sha"] in train_shas]
    train_missing = [
        name for name in FEATURES if not any(r["values"][name] is not None for r in selected_train)
    ]
    if selected_train and train_missing:
        deficits.append("train_all_missing_features")
    counts = Counter(r["label"] for r in selected_train)
    weights = (
        {name: len(selected_train) / (2 * counts[name]) for name in ("buggy", "clean")}
        if all(counts[n] for n in ("buggy", "clean"))
        else None
    )
    return {
        "ready": not deficits,
        "deficits": deficits,
        "label_version": LABEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "feature_rule_hash": RULE_HASH,
        "parser_version": snapshot.parser_version,
        "identity_version": snapshot.identity_version,
        "rule_hash": rule_hash(),
        "policy": asdict(policy),
        "head_sha": snapshot.head_sha,
        "repository_id": snapshot.repository_id,
        "source_digest": digest(
            {
                "feature": vectors[0].source_digest if vectors else None,
                "vectors": [v.as_dict() for v in sorted(vectors, key=lambda v: v.sha)],
                "observations": [asdict(o) for o in sorted(observations, key=lambda o: o.run_id)],
            }
        ),
        "labels": rows,
        "label_counts": dict(sorted(Counter(r["label"] for r in rows).items())),
        "unknown_reasons": dict(sorted(Counter(r["reason"] for r in rows if r["reason"]).items())),
        "split": partitions,
        "boundaries": boundaries,
        "purged": purged,
        "train_class_counts": dict(counts),
        "train_class_weights": weights,
        "train_all_missing_features": train_missing,
        "feature_missing_counts": {
            "population": {f: sum(r["values"][f] is None for r in rows) for f in FEATURES},
            "train": {f: sum(r["values"][f] is None for r in selected_train) for f in FEATURES},
        },
        "preprocessing": {
            "fit_split": "train",
            "imputation": "median",
            "scaling": "standard",
            "imbalance": "class_weight",
            "all_missing": "reject",
            "fitted": False,
        },
    }


def build(snapshot, vectors, observations, policy, tick=lambda: None):
    import json

    result = audit(snapshot, vectors, observations, policy, tick)
    if not result["ready"]:
        raise DatasetError("DATA_MATURE_SAMPLES_INSUFFICIENT")
    # The JSON is frozen; as_dict never lets a caller modify the stored value.
    return FrozenDataset(
        json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.isoformat(timespec="microseconds") + "Z",
            allow_nan=False,
        )
    )
