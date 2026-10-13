"""Synthetic observed timelines, not measured project label/accuracy ground truth."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta

import pytest

from packages.mining.dataset import DatasetError, Evidence, Link, Observation, Policy, audit, build
from packages.mining.features import FEATURES, FeatureVector, Snapshot
from tests.test_features import commit, sha


def timeline(count=30):
    commits = tuple(commit(i + 1, (i,) if i else (), day=i * 120) for i in range(count))
    snapshot = Snapshot("synthetic-time-fixture", commits[-1].sha, commits)
    vectors = tuple(
        FeatureVector(
            c.sha,
            c.event_time,
            "a" * 64,
            tuple((f, i % 2 if f == "FIX" else i + 1) for f in FEATURES),
            (),
        )
        for i, c in enumerate(commits)
    )
    rounds = []
    for i, c in enumerate(commits):
        observed = c.event_time + timedelta(days=90)
        records = tuple(
            Evidence(
                prior.sha,
                prior.event_time + timedelta(days=1),
                j % 4 == 1,
                "confirmed" if j % 4 == 1 else "unreviewed",
                0,
                True,
                True,
                True,
                "traced" if j % 4 == 1 else "not_candidate",
                (Link(commits[j - 1].sha, True),) if j % 4 == 1 else (),
            )
            for j, prior in enumerate(commits[: i + 1])
        )
        rounds.append(
            Observation(
                f"synthetic-round-{i:04d}",
                c.sha,
                observed,
                observed + timedelta(days=1),
                observed,
                "b" * 64,
                records,
            )
        )
    policy = Policy(rounds[-1].published_at)
    return snapshot, vectors, tuple(rounds), policy


def test_success_raw_immutable_hash_and_train_only_weights():
    source = timeline()
    result = audit(*source)
    assert result["ready"] and all(result["split"].values())
    assert result["label_counts"] == {"buggy": 8, "clean": 22}
    assert len(result["purged"]) == 1
    train = result["train_class_counts"]
    assert result["train_class_weights"] == {
        k: sum(train.values()) / (2 * v) for k, v in train.items()
    }
    dataset = build(*source)
    changed = dataset.as_dict()
    changed["labels"][0]["values"]["LA"] = 999
    assert dataset.as_dict()["labels"][0]["values"]["LA"] == 1
    assert (
        dataset.content_hash
        == build(
            source[0], tuple(reversed(source[1])), tuple(reversed(source[2])), source[3]
        ).content_hash
    )
    assert result["preprocessing"]["fitted"] is False
    with pytest.raises(FrozenInstanceError):
        dataset.canonical_json = "{}"


def test_single_late_observation_does_not_backdate_clean_or_repair_split():
    snapshot, vectors, rounds, policy = timeline()
    result = audit(snapshot, vectors, rounds[-1:], policy)
    assert not result["ready"]
    assert {r["split"] for r in result["purged"]} == {"train", "validation"}
    assert result["split"]["train"] == result["split"]["validation"] == []
    assert result["boundaries"] == audit(snapshot, vectors, rounds, policy)["boundaries"]
    with pytest.raises(DatasetError, match="DATA_MATURE_SAMPLES_INSUFFICIENT"):
        build(snapshot, vectors, rounds[-1:], policy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("review_status", "unreviewed"),
        ("current_review_matches", False),
        ("content_complete", False),
        ("issues_complete", False),
        ("trace_status", "partial"),
    ],
)
def test_unresolved_and_conflicts_never_become_negative(field, value):
    snapshot, vectors, rounds, policy = timeline(8)
    last = rounds[-1]
    altered = tuple(replace(e, **{field: value}) if e.sha == sha(2) else e for e in last.records)
    result = audit(snapshot, vectors, (replace(last, records=altered),), policy)
    assert result["labels"][0]["label"] == "unknown"
    assert result["labels"][0]["reason"]


def test_observation_start_not_szz_start_and_age_boundary():
    snapshot, vectors, rounds, policy = timeline(4)
    last = rounds[-1]
    stale = replace(
        last,
        observed_at=snapshot.commits[-1].event_time,
        published_at=policy.snapshot_at + timedelta(days=100),
        as_of=policy.snapshot_at + timedelta(days=100),
    )
    result = audit(snapshot, vectors, (stale,), Policy(stale.published_at))
    assert result["labels"][-1]["label"] == "unknown"
    assert result["labels"][-1]["reason"] == "observation_incomplete_or_unresolved"
    mature = audit(snapshot, vectors, (last,), policy)
    assert mature["labels"][-1]["label"] == "clean"
    assert mature["labels"][-1]["label_available_at"] == last.published_at
    immature = audit(snapshot, vectors, rounds, Policy(snapshot.commits[-1].event_time))
    assert immature["labels"][-1]["reason"] == "immature"


def test_future_proof_and_events_unknown_not_backfilled():
    snapshot, vectors, rounds, _ = timeline(4)
    result = audit(snapshot, vectors, rounds, Policy(datetime(2020, 1, 1)))
    assert result["label_counts"] == {"unknown": 4}
    assert result["labels"][1]["reason"] == "event_after_snapshot"


def test_equal_event_time_not_split_by_sha_and_strict_cutoff():
    snapshot, vectors, rounds, policy = timeline()
    target = audit(snapshot, vectors, rounds, policy)["boundaries"]["validation_start"]
    adjusted = tuple(
        replace(c, event_time=target) if c.sha == sha(21) else c for c in snapshot.commits
    )
    changed_vectors = tuple(
        replace(v, event_time=target) if v.sha == sha(21) else v for v in vectors
    )
    result = audit(replace(snapshot, commits=adjusted), changed_vectors, rounds, policy)
    assert not any(
        sha(21) in result["split"][a] and sha(22) in result["split"][b]
        for a in result["split"]
        for b in result["split"]
        if a != b
    )
    # Set an otherwise valid train proof exactly at the first validation event.
    last = replace(
        rounds[-1],
        published_at=target,
        observed_at=target,
        as_of=target,
        records=rounds[-1].records,
    )
    exact = audit(snapshot, vectors, (last,), policy)
    assert sha(1) not in exact["split"]["train"]


def test_missing_raw_preserved_and_all_missing_train_rejected():
    snapshot, vectors, rounds, policy = timeline()
    changed = tuple(
        replace(
            v,
            values=tuple((k, None if k == "FIX" else n) for k, n in v.values),
            reasons=(("FIX", "late_fix_evidence"),),
        )
        for v in vectors
    )
    result = audit(snapshot, changed, rounds, policy)
    assert not result["ready"] and result["train_all_missing_features"] == ["FIX"]
    assert all(r["values"]["FIX"] is None for r in result["labels"])


@pytest.mark.parametrize(
    "change,code",
    [
        ("duplicate_round", "DATA_DUPLICATE_OBSERVATION"),
        ("missing_record", "DATA_OBSERVATION_POPULATION_INVALID"),
        ("outside_head", "DATA_OBSERVATION_OUTSIDE_HEAD"),
        ("self_blame", "DATA_LINK_INVALID"),
        ("feature_coverage", "DATA_FEATURE_COVERAGE_INCOMPLETE"),
        ("duplicate_vector", "DATA_FEATURE_POPULATION_INVALID"),
    ],
)
def test_invalid_frozen_sources_rejected(change, code):
    snapshot, vectors, rounds, policy = timeline()
    if change == "duplicate_round":
        rounds += rounds[-1:]
    elif change == "missing_record":
        rounds = (replace(rounds[-1], records=rounds[-1].records[1:]),)
    elif change == "outside_head":
        rounds = (replace(rounds[-1], head_sha=sha(999)),)
    elif change == "self_blame":
        rounds = (
            replace(
                rounds[-1],
                records=tuple(
                    replace(e, links=(Link(e.sha, True),)) if e.candidate else e
                    for e in rounds[-1].records
                ),
            ),
        )
    elif change == "feature_coverage":
        snapshot = replace(snapshot, history_coverage="recent_window")
    else:
        vectors += vectors[-1:]
    with pytest.raises(DatasetError, match=code):
        audit(snapshot, vectors, rounds, policy)


@pytest.mark.parametrize(
    "values",
    [
        dict(window_days=0),
        dict(window_days=True),
        dict(train=float("nan")),
        dict(train=0.8),
        dict(test=0),
    ],
)
def test_policy_not_test_tuned_or_nonfinite(values):
    with pytest.raises(DatasetError):
        Policy(datetime(2026, 1, 1), **values)


def test_supervision_interrupts_without_output():
    source = timeline()

    def stopped():
        raise DatasetError("DATA_AUDIT_TIMEOUT")

    with pytest.raises(DatasetError, match="DATA_AUDIT_TIMEOUT"):
        build(*source, tick=stopped)


def test_later_unreviewed_candidate_blocks_earlier_negative_proof():
    snapshot, vectors, rounds, policy = timeline(8)
    changed = replace(
        rounds[-1],
        records=tuple(
            replace(e, candidate=True, review_status="unreviewed", trace_status="traced", links=())
            if e.sha == sha(4)
            else e
            for e in rounds[-1].records
        ),
    )
    result = audit(snapshot, vectors, rounds[:-1] + (changed,), policy)
    assert result["labels"][2]["label"] == "unknown"
    assert result["labels"][2]["reason"] == "unresolved_evidence"


def test_limited_observation_never_proves_clean():
    snapshot, vectors, rounds, policy = timeline(4)
    result = audit(
        snapshot,
        vectors,
        tuple(replace(o, history_coverage="recent_window") for o in rounds),
        policy,
    )
    assert result["label_counts"] == {"unknown": 4}


def test_content_hash_binds_actual_raw_values_and_window():
    snapshot, vectors, rounds, policy = timeline()
    original = build(snapshot, vectors, rounds, policy)
    changed = (
        replace(
            vectors[0], values=tuple((k, 20 if k == "LA" else v) for k, v in vectors[0].values)
        ),
    ) + vectors[1:]
    assert original.content_hash != build(snapshot, changed, rounds, policy).content_hash
    assert (
        original.content_hash
        != build(snapshot, vectors, rounds, replace(policy, window_days=89)).content_hash
    )
