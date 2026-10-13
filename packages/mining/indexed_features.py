"""Bounded v2 acceleration; legacy kernel remains the equivalence oracle."""

import math
from bisect import bisect_left
from dataclasses import asdict, replace
from pathlib import Path

from packages.mining.features import (
    FEATURE_VERSION,
    HISTORY,
    RULE_HASH,
    FeatureVector,
    ancestry,
    digest,
    subsystem,
    vector,
)


def rule_hash():
    return digest({"legacy": RULE_HASH, "index": Path(__file__).read_text(encoding="utf-8")})


def compute_indexed(snapshot, tick=lambda: None):
    """Same values/reasons/source as historical compute; caller enforces v2 limits.

    Index only file events, then intersect with strict DAG ancestry per commit.
    Author decay and ancestry still have quadratic worst-case cost. No size-limit
    increase or full-history certification is implied by this optimization.
    """
    ordered = sorted(snapshot.commits, key=lambda c: c.order)
    index = {c.sha: c for c in ordered}
    frozen, paths = [], {}
    for ordinal, commit in enumerate(ordered):
        tick()
        frozen.append(asdict(commit))
        # Reverse file insertion so reverse traversal retains original file order.
        for changed in reversed(commit.files):
            path = changed.new_path if changed.new_path is not None else changed.old_path
            paths.setdefault(path, []).append((ordinal, commit, changed))
    source = digest(
        {
            "repository_id": snapshot.repository_id,
            "head_sha": snapshot.head_sha,
            "coverage": snapshot.history_coverage,
            "parser_version": snapshot.parser_version,
            "identity_version": snapshot.identity_version,
            "feature_version": FEATURE_VERSION,
            "rule_hash": RULE_HASH,
            "purpose": "historical",
            "commits": frozen,
        }
    )
    window = replace(snapshot, history_coverage="recent_window")
    linear = all(
        c.parents == (() if i == 0 else (ordered[i - 1].sha,)) for i, c in enumerate(ordered)
    )
    # High-frequency paths benefit from the legacy single reverse scan. Both
    # strategies are exact, selected only from this immutable input population.
    dense = max((len(events) for events in paths.values()), default=0) > len(ordered) / 4
    output = []
    for ordinal, commit in enumerate(ordered):
        tick()
        previous = (
            []
            if snapshot.history_coverage != "full"
            else ordered[:ordinal]
            if linear
            else ancestry(commit, index, tick)
        )
        if dense:
            output.append(vector(commit, previous, snapshot, source, "historical", tick))
            continue
        base = vector(commit, [], window, source, "historical", tick)
        values, reasons = dict(base.values), dict(base.reasons)
        valid = (
            commit.parse_status == "parsed"
            and len(commit.parents) <= 1
            and all(f.supported for f in commit.files)
        )
        if valid and snapshot.history_coverage == "full":
            for name in HISTORY:
                reasons.pop(name, None)

            def missing(keys, reason):
                for name in keys:
                    values[name], reasons[name] = None, reason

            if any(c.order >= commit.order for c in previous):
                missing(HISTORY, "ancestor_time_inversion")
            else:
                authors = [c for c in previous if c.identity_key == commit.identity_key]
                values["EXP"] = len(authors)
                values["REXP"] = math.fsum(
                    1 / (1 + (commit.event_time - c.event_time).total_seconds() / (86400 * 365.25))
                    for c in authors
                )
                incomplete = any(
                    c.parse_status != "parsed"
                    or len(c.parents) > 1
                    or any(not f.supported for f in c.files)
                    for c in previous
                )
                if incomplete:
                    missing(("SEXP", "NDEV", "AGE", "NUC"), "ancestor_content_unsupported")
                else:
                    systems = {subsystem(f.path) for f in commit.files}
                    values["SEXP"] = sum(
                        bool(systems & {subsystem(f.path) for f in c.files}) for c in authors
                    )
                    accepted = {c.sha for c in previous}

                    def history(file):
                        if file.change_type in {"A", "C"}:
                            return []
                        path, before, result = file.old_path, ordinal, []
                        scanned = 0
                        while True:
                            found = False
                            events = paths.get(path, ())
                            end = bisect_left(events, before, key=lambda event: event[0])
                            for event_index in range(end - 1, -1, -1):
                                if scanned % 256 == 0:
                                    tick()
                                scanned += 1
                                number, prior, changed = events[event_index]
                                if prior.sha not in accepted:
                                    continue
                                if changed.new_path == path:
                                    result.append(prior)
                                    if changed.change_type in {"A", "C"}:
                                        return result
                                    path, before, found = changed.old_path, number, True
                                    break
                                if changed.change_type == "D" and changed.old_path == path:
                                    return result
                            if not found:
                                return result

                    histories = [history(f) for f in commit.files]
                    churn = [f.insertions + f.deletions for f in commit.files]
                    total = sum(churn)
                    values["NDEV"] = len({c.identity_key for h in histories for c in h})
                    values["NUC"] = len({h[0].sha for h in histories if h})
                    if not total:
                        missing(("AGE",), "zero_churn")
                    elif any(not h for h, weight in zip(histories, churn, strict=True) if weight):
                        missing(("AGE",), "first_file_change")
                    else:
                        values["AGE"] = (
                            math.fsum(
                                weight
                                * (commit.event_time - h[0].event_time).total_seconds()
                                / 86400
                                for h, weight in zip(histories, churn, strict=True)
                                if weight
                            )
                            / total
                        )
        output.append(
            FeatureVector(
                commit.sha,
                commit.event_time,
                source,
                tuple(values.items()),
                tuple(sorted(reasons.items())),
            )
        )
    return tuple(output)
