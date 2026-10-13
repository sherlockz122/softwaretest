"""Counterfactual engineering fixtures and real Git purpose signals, never accuracy evidence."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

from packages.mining.causal_features import (
    VERSION,
    CausalFeatureError,
    CausalRun,
    MessagePurpose,
    compute_causal,
    freeze_message,
)
from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import FEATURES, FixSignal, Snapshot, compute
from packages.mining.historical_replay import (
    FixLink,
    ReplayError,
    ReplayOutcome,
    ReplayPolicy,
    ReplaySource,
    audit_replay,
)
from tests.szz_support import reader_for, szz_history
from tests.test_features import START, change, commit, sha


@pytest.fixture
def case():
    commits = tuple(
        commit(
            i + 1,
            (i,) if i else (),
            day=1 + 10 * i,
            files=[change(None, "src/code.py", "A", ld=0, lt=0)] if not i else None,
        )
        for i in range(20)
    )
    snapshot = Snapshot("fixture", commits[-1].sha, commits)
    messages = tuple(
        MessagePurpose(c.sha, i % 2, "a" * 64, "message_purpose") for i, c in enumerate(commits)
    )
    run = compute_causal(snapshot, messages)
    source = ReplaySource(
        "fixture",
        snapshot.head_sha,
        START + timedelta(days=365),
        "b" * 64,
        "c" * 64,
        START,
        START + timedelta(days=225),
        True,
        True,
    )
    policy = ReplayPolicy(source.coverage_end, 2, 0)
    outcomes = tuple(
        ReplayOutcome(
            c.sha,
            (FixLink(sha(1000 + i), c.event_time + timedelta(days=1), True),) if i % 2 else (),
        )
        for i, c in enumerate(commits)
    )
    return snapshot, messages, run, source, policy, outcomes


def evaluate(case, **kwargs):
    _, _, run, source, policy, outcomes = case
    return audit_replay(
        kwargs.get("run", run),
        kwargs.get("outcomes", outcomes),
        kwargs.get("source", source),
        kwargs.get("policy", policy),
    ).as_dict()


def row(result, number):
    return next(r for r in result["rows"] if r["sha"] == sha(number))


def test_replay_has_two_classes_and_train_only_weights(case):
    result = evaluate(case)
    assert result["replay_structurally_ready"] is True
    assert result["product_training_ready"] is False
    assert result["labels"] == {"buggy": 10, "clean": 10}
    assert result["splits"]["train"]["classes"] == {"buggy": 7, "clean": 7}
    assert result["splits"]["train"]["class_weights"] == {"buggy": 1, "clean": 1}
    assert result["preprocessing"]["fitted"] is False


def test_future_fix_reviews_cannot_change_causal_inputs_or_hash(case):
    snapshot, messages, run, *_ = case
    late = FixSignal(True, START + timedelta(days=300), "future-review", 8, "e" * 64)
    future = replace(snapshot, commits=tuple(replace(c, fix=late) for c in snapshot.commits))
    assert compute_causal(future, messages).content_hash == run.content_hash
    original = compute(future)
    assert all(dict(v.values)["FIX"] is None for v in original)
    assert dict(original[-1].reasons)["FIX"] == "fix_not_yet_available"


def test_numeric_values_and_missing_reasons_keep_existing_rules(case):
    snapshot, _, run, *_ = case
    for old, new in zip(compute(snapshot), run.as_dict()["vectors"], strict=True):
        for key in set(FEATURES) - {"FIX"}:
            assert dict(old.values)[key] == new["values"][key]
            assert dict(old.reasons).get(key) == new["reasons"].get(key)
    assert run.as_dict()["feature_version"] == VERSION


@pytest.mark.parametrize(
    "message,status,value",
    [
        ("Fix a crash", "parsed", 1),
        ("Add a feature", "parsed", 0),
        ("This does not fix the bug", "parsed", 0),
        ("Revert fix", "parsed", 0),
        ("fix\ufffd", "message_encoding", None),
        ("Fix a crash", "merge_skipped", None),
    ],
)
def test_message_purpose_is_not_external_buggy_label(message, status, value):
    record = {"commit": {"sha": sha(1), "message": message, "parse_status": status}, "files": []}
    result = freeze_message(record)
    assert result.value == value
    assert message not in str(result)


def test_real_git_message_and_lt_are_available_without_later_review(tmp_path):
    repo, _, _, _ = szz_history(tmp_path / "source", scenario="parent")
    reader = reader_for(repo, tmp_path / "reader")
    head = reader.command(["rev-parse", "HEAD"]).decode().strip()
    records = [reader.record(s) for s in reader.plan(head, None)]
    inputs = tuple(freeze_native_record(reader, r) for r in records)
    run = compute_causal(
        Snapshot("real-git-fixture", head, inputs), tuple(freeze_message(r) for r in records)
    ).as_dict()
    last = run["vectors"][-1]
    assert last["values"]["FIX"] == 1
    assert last["values"]["LT"] == 1
    assert last["values"]["EXP"] == 2
    assert "fix_not_yet_available" not in last["reasons"].values()


def test_frozen_runs_return_independent_copies(case):
    run = case[2]
    first = run.as_dict()
    first["vectors"][0]["values"]["FIX"] = 100
    assert run.as_dict()["vectors"][0]["values"]["FIX"] == 0
    with pytest.raises(FrozenInstanceError):
        run.canonical_json = "{}"
    result = audit_replay(case[2], case[5], case[3], case[4])
    mutated = result.as_dict()
    mutated["rows"][0]["label"] = "fake"
    assert result.as_dict()["rows"][0]["label"] == "clean"


def test_order_is_deterministic_and_source_changes_make_new_version(case):
    snap, messages, run, source, policy, outcomes = case
    reversed_run = compute_causal(
        replace(snap, commits=tuple(reversed(snap.commits))), tuple(reversed(messages))
    )
    assert reversed_run.content_hash == run.content_hash
    first = audit_replay(run, outcomes, source, policy)
    assert (
        audit_replay(run, tuple(reversed(outcomes)), source, policy).content_hash
        == first.content_hash
    )
    assert (
        audit_replay(run, outcomes, replace(source, evidence_digest="d" * 64), policy).content_hash
        != first.content_hash
    )


@pytest.mark.parametrize(
    "complete,verified,reason",
    [
        (False, True, "negative_censored_or_incomplete"),
        (True, False, "clock_unverified"),
    ],
)
def test_incomplete_negative_coverage_and_uncertain_clock_remain_unknown(
    case, complete, verified, reason
):
    source = replace(case[3], coverage_complete=complete, timezone_verified=verified)
    result = evaluate(case, source=source)
    assert row(result, 1)["label"] == "Unknown"
    assert row(result, 1)["label_reason"] == reason
    assert result["replay_structurally_ready"] is False


def test_missing_outcome_is_not_automatically_clean(case):
    result = evaluate(case, outcomes=case[5][1:])
    assert row(result, 1)["label_reason"] == "outcome_unavailable"


@pytest.mark.parametrize("kind", ["unresolved", "ancestry", "inversion", "self", "future"])
def test_positive_evidence_conflicts_are_not_silently_discarded(case, kind):
    outcomes = list(case[5])
    target = outcomes[1]
    link = target.links[0]
    if kind == "unresolved":
        target = replace(target, unresolved=True)
    elif kind == "ancestry":
        target = replace(target, links=(replace(link, ancestry_verified=False),))
    elif kind == "inversion":
        target = replace(target, links=(replace(link, fix_time=START),))
    elif kind == "self":
        target = replace(target, links=(replace(link, fix_sha=target.sha),))
    else:
        target = replace(
            target, links=(replace(link, fix_time=case[3].coverage_end + timedelta(days=1)),)
        )
    outcomes[1] = target
    assert row(evaluate(case, outcomes=tuple(outcomes)), 2)["label"] == "Unknown"


def test_late_labels_are_purged_without_moving_predeclared_boundaries(case):
    normal = evaluate(case)
    delayed = evaluate(case, policy=replace(case[4], knowledge_lag_days=10))
    assert delayed["purged"]
    for name in ("train", "validation", "test"):
        assert normal["splits"][name]["membership"] == delayed["splits"][name]["membership"]
    assert delayed["splits"]["train"]["classes"] == {"buggy": 6, "clean": 7}
    assert {p["sha"] for p in delayed["purged"]} == {sha(14), sha(17)}


def test_negative_endpoint_does_not_mean_never_buggy(case):
    outcomes = list(case[5])
    commit = case[0].commits[0]
    outcomes[0] = replace(
        outcomes[0], links=(FixLink(sha(999), commit.event_time + timedelta(days=3), True),)
    )
    result = evaluate(case, outcomes=tuple(outcomes))
    assert row(result, 1)["label"] == "clean"  # Explicitly only within W=2 days.
    assert row(result, 1)["label_reason"] == "no_observed_link_within_horizon"


def test_horizon_fix_is_inclusive(case):
    outcomes = list(case[5])
    event = case[0].commits[0].event_time
    outcomes[0] = replace(outcomes[0], links=(FixLink(sha(999), event + timedelta(days=2), True),))
    assert row(evaluate(case, outcomes=tuple(outcomes)), 1)["label"] == "buggy"


def test_recent_horizon_and_future_events_are_censored(case):
    policy = replace(case[4], as_of=START + timedelta(days=11))
    source = replace(case[3], coverage_end=policy.as_of)
    outcomes = (case[5][0], replace(case[5][1], links=()), *case[5][2:])
    result = evaluate(case, source=source, policy=policy, outcomes=outcomes)
    assert row(result, 2)["label_reason"] == "negative_censored_or_incomplete"
    assert row(result, 3)["label_reason"] == "future_event"


def test_same_event_groups_never_cross_partitions(case):
    snap, messages, *_ = case
    commits = tuple(
        replace(c, event_time=START + timedelta(days=10 * (i // 2)))
        for i, c in enumerate(snap.commits)
    )
    run = compute_causal(replace(snap, commits=commits), messages)
    outcomes = tuple(ReplayOutcome(c.sha) for c in commits)
    result = evaluate(case, run=run, outcomes=outcomes)
    mapping = {sha: name for name, s in result["splits"].items() for sha in s["membership"]}
    for a, b in zip(commits[::2], commits[1::2], strict=True):
        assert mapping[a.sha] == mapping[b.sha]


def test_short_history_keeps_six_missing_and_cannot_train(case):
    snap, messages, *_ = case
    run = compute_causal(replace(snap, history_coverage="recent_window"), messages)
    assert "NDEV" in run.as_dict()["vectors"][-1]["missing"]
    result = evaluate(case, run=run)
    assert "train_all_missing_features" in result["deficits"]


@pytest.mark.parametrize("change", ["duplicate", "missing", "extra"])
def test_message_population_must_match_snapshot(case, change):
    snap, messages, *_ = case
    values = (
        (*messages, messages[0])
        if change == "duplicate"
        else messages[1:]
        if change == "missing"
        else (*messages, replace(messages[0], sha=sha(999)))
    )
    with pytest.raises(CausalFeatureError, match="CAUSAL_MESSAGE_POPULATION"):
        compute_causal(snap, tuple(values))


def test_source_must_match_same_repository_head(case):
    with pytest.raises(ReplayError, match="REPLAY_SOURCE_CONFLICT"):
        evaluate(case, source=replace(case[3], repository="other"))


@pytest.mark.parametrize("wrong", ["{}", "[]", '{"feature_version":"old"}'])
def test_untrusted_or_incompatible_vectors_rejected(case, wrong):
    with pytest.raises(ReplayError):
        evaluate(case, run=CausalRun(wrong))


def test_values_cannot_be_nan_or_missing_without_reason(case):
    data = case[2].as_dict()
    data["vectors"][0]["values"]["LT"] = -1
    with pytest.raises(ReplayError, match="REPLAY_VECTOR_INVALID"):
        evaluate(case, run=CausalRun(json.dumps(data)))


def test_no_duplicate_or_unrelated_evidence(case):
    with pytest.raises(ReplayError, match="REPLAY_DUPLICATE_SHA"):
        evaluate(case, outcomes=(*case[5], case[5][0]))
    with pytest.raises(ReplayError, match="REPLAY_POPULATION_INVALID"):
        evaluate(case, outcomes=(*case[5], ReplayOutcome(sha(999))))


def test_bounded_features_and_timeout_preserve_caller_inputs(case, monkeypatch):
    snap, messages, run, *_ = case
    with pytest.raises(CausalFeatureError, match="CAUSAL_COMMIT_LIMIT"):
        compute_causal(snap, messages, max_commits=10)
    stamps = iter((0, 31))
    monkeypatch.setattr("packages.mining.causal_features.time.perf_counter", lambda: next(stamps))
    with pytest.raises(CausalFeatureError, match="CAUSAL_TIMEOUT"):
        compute_causal(snap, messages)
    assert run.as_dict()["vectors"][0]["values"]["FIX"] == 0


def test_replay_timeout_and_cancel_propagate(case, monkeypatch):
    stamps = iter((0, 31))
    monkeypatch.setattr("packages.mining.historical_replay.time.perf_counter", lambda: next(stamps))
    with pytest.raises(ReplayError, match="REPLAY_TIMEOUT"):
        evaluate(case)


def test_cancellation_is_not_swallowed(case):
    def cancel():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        audit_replay(case[2], case[5], case[3], case[4], tick=cancel)


@pytest.mark.parametrize("days,lag", [(0, 0), (3651, 0), (90, -1), (90, 3651), (True, 1)])
def test_invalid_policy_rejected(days, lag):
    with pytest.raises(ReplayError):
        ReplayPolicy(START, days, lag)


def test_label_equal_to_train_cutoff_is_not_known_strictly_before(case):
    result = evaluate(case, policy=replace(case[4], knowledge_lag_days=9))
    assert {"sha": sha(14), "split": "train", "reason": "label_after_cutoff"} in result["purged"]


def test_late_positive_not_available_for_evaluation(case):
    policy = replace(case[4], as_of=START + timedelta(days=12), knowledge_lag_days=1)
    source = replace(case[3], coverage_end=policy.as_of)
    result = evaluate(case, policy=policy, source=source)
    assert row(result, 2)["label_reason"] == "positive_not_yet_known"
    assert row(result, 2)["label"] == "Unknown"


def test_native_probe_reads_owned_git_without_inventing_complete_coverage(tmp_path, monkeypatch):
    from scripts import fetch_public_dataset, probe_causal_features

    monkeypatch.setattr(fetch_public_dataset, "ROOT", tmp_path)
    monkeypatch.setattr(probe_causal_features, "ROOT", tmp_path)
    source_path = tmp_path / "runtime/source"
    source_path.parent.mkdir()
    repo, _, _, _ = szz_history(source_path, scenario="parent")
    reader = reader_for(repo, tmp_path / "reader")
    head = reader.command(["rev-parse", "HEAD"]).decode().strip()
    before = reader.command(["show-ref"])
    result = probe_causal_features.probe(repo, head, "fixture", "2021-01-01T00:00:00Z", 3)
    assert result["replay"]["labels"] == {"Unknown": 3}
    assert result["features"]["vectors"][-1]["values"]["FIX"] == 1
    assert result["features"]["vectors"][-1]["values"]["LT"] == 1
    assert result["features"]["vectors"][-1]["values"]["EXP"] is None
    assert reader.command(["show-ref"]) == before


def test_local_probe_rejects_alternates_and_outside_runtime(tmp_path, monkeypatch):
    from scripts import fetch_public_dataset, probe_causal_features

    monkeypatch.setattr(fetch_public_dataset, "ROOT", tmp_path)
    monkeypatch.setattr(probe_causal_features, "ROOT", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="runtime"):
        probe_causal_features.checked_path(outside)
    bare = tmp_path / "runtime/source"
    (bare / "objects/info").mkdir(parents=True)
    (bare / "objects/info/alternates").write_text("untrusted")
    with pytest.raises(ValueError, match="REPLAY_GIT_ALTERNATES"):
        probe_causal_features.probe(bare, sha(1), "fixture", "2021-01-01T00:00:00Z")


def test_undecodable_merge_message_cannot_become_a_known_purpose(tmp_path):
    from tests.repository_support import git

    repo, _, _, shas = szz_history(tmp_path / "source", scenario="parent")
    tree = git("-C", str(repo), "rev-parse", "HEAD^{tree}").decode().strip()
    merge = (
        git(
            "-C",
            str(repo),
            "commit-tree",
            tree,
            "-p",
            shas[-1],
            "-p",
            shas[0],
            data=b"Fix a crash \xff\n",
        )
        .decode()
        .strip()
    )
    headers = git("-C", str(repo), "cat-file", "commit", merge).partition(b"\n\n")[0]
    headers = b"\n".join(line for line in headers.splitlines() if not line.startswith(b"encoding "))
    raw = headers + b"\n\nFix a crash \xff\n"
    merge = (
        git("-C", str(repo), "hash-object", "-w", "-t", "commit", "--stdin", data=raw)
        .decode()
        .strip()
    )
    with pytest.raises(UnicodeDecodeError):
        git("-C", str(repo), "cat-file", "commit", merge).partition(b"\n\n")[2].decode("utf-8")
    record = reader_for(repo, tmp_path / "reader").record(merge)
    assert record["commit"]["parse_status"] == "merge_skipped"
    assert "\ufffd" in record["commit"]["message"]
    result = freeze_message(record)
    assert result.value is None
    assert result.reason == "message_unverified"
