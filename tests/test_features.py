"""Hand calculated real Git and causal/lineage/missing-value feature boundaries."""

import json
import os
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta

import pytest

from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import (
    FEATURES,
    HISTORY,
    CommitInput,
    FeatureInputError,
    FileDelta,
    FixSignal,
    Snapshot,
    compute,
    freeze_record,
    quality,
)
from tests.repository_support import git
from tests.szz_support import reader_for, szz_history

START = datetime(2020, 1, 1)
ALICE, BOB = "a" * 64, "b" * 64


def sha(number):
    return f"{number:040x}"


def change(old="src/code.py", new="src/code.py", kind="M", la=1, ld=1, lt=3, **kwargs):
    return FileDelta(old, new, kind, la, ld, lt, **kwargs)


def commit(number, parents=(), day=0, files=None, author=ALICE, **kwargs):
    return CommitInput(
        sha(number),
        tuple(sha(n) for n in parents),
        START + timedelta(days=day),
        author,
        tuple(files if files is not None else [change()]),
        **kwargs,
    )


def snapshot(*commits, **kwargs):
    return Snapshot("fixture", commits[-1].sha, tuple(commits), **kwargs)


def result(*commits, **kwargs):
    return compute(snapshot(*commits, **kwargs))[-1].as_dict()


def test_fourteen_hand_calculated_values_from_actual_git(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git("init", "-b", "fixture", str(root))
    git("-C", str(root), "config", "core.autocrlf", "false")
    a, b = root / "app/a.py", root / "lib/b.py"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_text("a=1\nb=1\nc=1\n", encoding="utf-8")
    b.write_text("x=1\ny=1\n", encoding="utf-8")
    ids = []

    def save(day, author):
        seconds = 1577836800 + day * 86400
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME=author,
            GIT_COMMITTER_NAME=author,
            GIT_AUTHOR_EMAIL=author + "@example.invalid",
            GIT_COMMITTER_EMAIL=author + "@example.invalid",
            GIT_AUTHOR_DATE=f"{seconds} +0000",
            GIT_COMMITTER_DATE=f"{seconds} +0000",
        )
        git("-C", str(root), "add", "--all")
        git("-C", str(root), "commit", "-m", "Fixture", env=env)
        ids.append(git("-C", str(root), "rev-parse", "HEAD").decode().strip())

    save(0, "Alice")
    a.write_text("a=2\nb=1\nc=1\n", encoding="utf-8")
    save(1, "Bob")
    a.write_text("a=3\nb=1\nc=1\nmore=1\n", encoding="utf-8")
    b.write_text("x=2\ny=1\n", encoding="utf-8")
    save(3, "Alice")
    reader = reader_for(root, tmp_path / "attempt")
    records = [reader.record(value) for value in ids]
    fixed = FixSignal(True, START + timedelta(days=3), "fixture-rule-v1", 1, "f" * 64)
    inputs = tuple(
        freeze_native_record(reader, r, fixed if i == 2 else None) for i, r in enumerate(records)
    )
    outputs = compute(Snapshot("actual-git", ids[-1], inputs))
    expected = {
        "NS": 2,
        "ND": 2,
        "NF": 2,
        "Entropy": 0.9709505944546686,
        "LA": 3,
        "LD": 2,
        "LT": 5,
        "FIX": 1,
        "NDEV": 2,
        "AGE": 2.4,
        "NUC": 2,
        "EXP": 1,
        "REXP": 1 / (1 + 3 / 365.25),
        "SEXP": 1,
    }
    assert outputs[-1].as_dict()["values"] == pytest.approx(expected)
    assert outputs[-1].as_dict()["missing"] == []
    assert tuple(expected) == FEATURES
    assert outputs == compute(Snapshot("actual-git", ids[-1], tuple(reversed(inputs))))
    # The adapter freezes a projection; later mutable parser dictionaries have no effect.
    records[-1]["files"][0]["insertions"] = 999
    records[-1]["identity"]["name_alias"] = "Different"
    assert compute(Snapshot("actual-git", ids[-1], inputs)) == outputs
    exported = json.dumps([v.as_dict() for v in outputs])
    assert "@" not in exported and "Alice" not in exported and "diff_text" not in exported
    assert quality(outputs)["features"]["AGE"]["missing_reasons"] == {"first_file_change": 1}


@pytest.mark.parametrize("scenario", ["rename", "delete_readd", "copy", "new_file"])
def test_lineage_and_new_lifetime(scenario):
    first = commit(1, files=[change(None, "src/old.py", "A", 3, 0, 0)])
    if scenario == "rename":
        second = commit(2, [1], 1, [change("src/old.py", "src/new.py", "R", 0, 0)])
        target = commit(3, [2], 4, [change("src/new.py", "src/new.py")], BOB)
        values = result(first, second, target)["values"]
        assert (values["NDEV"], values["NUC"], values["AGE"]) == (1, 1, 3)
    elif scenario == "delete_readd":
        deleted = commit(2, [1], 1, [change("src/old.py", None, "D", 0, 3)], BOB)
        added = commit(3, [2], 2, [change(None, "src/old.py", "A", 2, 0, 0)])
        target = commit(4, [3], 4, [change("src/old.py", "src/old.py")])
        values = result(first, deleted, added, target)["values"]
        assert (values["NDEV"], values["NUC"], values["AGE"]) == (1, 1, 2)
        assert result(first, deleted, added)["reasons"]["AGE"] == "first_file_change"
    else:
        file = change(
            "src/old.py" if scenario == "copy" else None,
            "src/new.py",
            "C" if scenario == "copy" else "A",
            3,
            0,
            0,
        )
        target = commit(2, [1], 1, [file])
        values = result(first, target)["values"]
        assert (values["NDEV"], values["NUC"], values["AGE"]) == (0, 0, None)


def test_nuc_counts_last_changes_not_entire_history_or_file_sum():
    files = [change("src/a.py", "src/a.py"), change("src/b.py", "src/b.py")]
    first = commit(1, files=files)
    second = commit(2, [1], 1, files, BOB)
    third = commit(3, [2], 2, files)
    target = commit(4, [3], 5, files)
    values = result(first, second, third, target)["values"]
    assert (values["NUC"], values["NDEV"], values["EXP"], values["SEXP"]) == (1, 2, 2, 2)
    assert values["AGE"] == 3


@pytest.mark.parametrize("coverage", ["full", "recent_window"])
def test_complete_first_commit_zero_and_recent_history_missing(coverage):
    root = commit(1, files=[change(None, "new.py", "A", 3, 0, 0)])
    value = result(root, history_coverage=coverage)
    assert value["values"]["LA"] == 3 and value["values"]["LT"] == 0
    for name in HISTORY:
        if coverage == "recent_window":
            assert value["values"][name] is None and value["reasons"][name] == "history_incomplete"
        elif name != "AGE":
            assert value["values"][name] == 0
    assert value["values"]["AGE"] is None


@pytest.mark.parametrize("kind", ["empty", "rename", "mode"])
def test_zero_churn_has_finite_entropy_and_explicit_age(kind):
    first = commit(1)
    files = [] if kind == "empty" else [change(la=0, ld=0)]
    if kind == "rename":
        files = [change("src/code.py", "lib/new.py", "R", 0, 0)]
    if kind == "mode":
        files = [change(kind="T", la=0, ld=0)]
    values = result(first, commit(2, [1], 1, files))
    if kind == "mode":
        assert values["reasons"]["Entropy"] == "current_content_unsupported"
    else:
        assert values["values"]["Entropy"] == 0 and values["values"]["AGE"] is None
        assert values["reasons"]["AGE"] == "zero_churn"


@pytest.mark.parametrize("status", ["binary", "encoding", "blob_limit", "diff_limit", "submodule"])
def test_unsupported_current_and_ancestor_do_not_become_zero(status):
    bad = change(content_status=status, is_binary=status == "binary")
    first = commit(1, files=[bad])
    current = result(first)
    assert current["values"]["LA"] is None
    assert current["values"]["NF"] is None
    target = result(first, commit(2, [1], 1))
    for name in ("AGE", "NDEV", "NUC", "SEXP"):
        assert target["values"][name] is None
        assert target["reasons"][name] == "ancestor_content_unsupported"
    assert target["values"]["EXP"] == 1 and target["values"]["REXP"] > 0


def test_non_ancestor_branch_cannot_contribute_to_history():
    base = commit(1)
    side = commit(2, [1], 1, author=BOB)
    target = commit(3, [1], 2)
    merged = commit(4, [2, 3], 3, [], parse_status="merge_skipped")
    vectors = compute(snapshot(base, side, target, merged))
    values = next(v for v in vectors if v.sha == target.sha).as_dict()["values"]
    assert (values["NDEV"], values["EXP"], values["NUC"]) == (1, 1, 1)
    merged_values = vectors[-1].as_dict()
    assert merged_values["values"]["LA"] is None
    child = commit(5, [4], 4)
    child_values = result(base, side, target, merged, child)["values"]
    assert child_values["EXP"] == 3 and child_values["NUC"] is None


@pytest.mark.parametrize("boundary", ["earlier_sha", "later_sha", "future_time"])
def test_event_time_sha_boundary(boundary):
    base = commit(1 if boundary == "earlier_sha" else 3, day=1 if boundary == "future_time" else 0)
    target = commit(2, [int(base.sha, 16)], day=0)
    values = result(base, target)["values"]
    if boundary == "earlier_sha":
        assert values["EXP"] == 1 and values["AGE"] == 0
    else:
        vector = next(v for v in compute(snapshot(base, target)) if v.sha == target.sha).as_dict()
        assert all(vector["values"][key] is None for key in HISTORY)
        assert vector["reasons"]["EXP"] == "ancestor_time_inversion"
        assert vector["values"]["LA"] == 1


@pytest.mark.parametrize("offset,expected", [(-1, 1), (0, 1), (1, None)])
def test_fix_availability_is_checked_without_backdating(offset, expected):
    signal = FixSignal(True, START + timedelta(seconds=offset), "fix-rule", 0, "f" * 64)
    root = commit(1, fix=signal)
    historical = result(root)
    assert historical["values"]["FIX"] == expected
    candidate = compute(snapshot(root), purpose="candidate")[0].as_dict()
    assert candidate["values"]["FIX"] == 0
    assert candidate["reasons"]["FIX"] == "candidate_fix_default"
    assert candidate["source_digest"] != historical["source_digest"]


@pytest.mark.parametrize(
    "case,code",
    [
        ("missing_parent", "FEATURE_HISTORY_INCOMPLETE"),
        ("duplicate", "FEATURE_DUPLICATE_SHA"),
        ("extra", "FEATURE_UNACCEPTED_COMMIT"),
        ("cycle", "FEATURE_DAG_CYCLE"),
        ("version", "FEATURE_VERSION_CONFLICT"),
        ("head", "FEATURE_HEAD_MISSING"),
        ("mutable", "FEATURE_FROZEN_INPUT_REQUIRED"),
    ],
)
def test_snapshot_input_rejects_invalid_state(case, code):
    a, b = commit(1), commit(2, [1], 1)
    kwargs = {}
    commits = (a, b)
    head = b.sha
    if case == "missing_parent":
        commits = (b,)
    elif case == "duplicate":
        commits = (a, b, b)
    elif case == "extra":
        commits = (a, b, commit(3))
    elif case == "cycle":
        commits = (replace(a, parents=(b.sha,)), b)
    elif case == "version":
        kwargs["identity_version"] = "unversioned"
    elif case == "head":
        head = sha(99)
    elif case == "mutable":
        commits = list(commits)
    with pytest.raises(FeatureInputError, match=code):
        Snapshot("fixture", head, commits, **kwargs)
    if case == "missing_parent":
        value = compute(Snapshot("fixture", head, commits, history_coverage="recent_window"))[0]
        assert dict(value.values)["EXP"] is None


@pytest.mark.parametrize("path", ["../x.py", "a/../x", "/x", "", "a//x", "x\x00.py"])
def test_invalid_paths_have_stable_error(path):
    with pytest.raises(FeatureInputError, match="FEATURE_PATH_INVALID"):
        change(path, path)


def test_git_path_case_and_backslash_are_not_windows_normalized():
    files = [change(None, path, "A", 1, 0, 0) for path in ("Src/A.py", "src/a.py", "root\\file.py")]
    values = result(commit(1, files=files))["values"]
    assert (values["NS"], values["ND"], values["NF"]) == (3, 3, 3)
    # NF=3 distinguishes normalized entropy (1) from raw log2(3); NF=2 cannot.
    assert values["Entropy"] == pytest.approx(1.0)


def test_immutable_output_and_versioned_digest():
    a, b = commit(1), commit(2, [1], 1)
    original = snapshot(a, b)
    outputs = compute(original)
    copy = outputs[0].as_dict()
    copy["values"]["LA"] = 999
    assert dict(outputs[0].values)["LA"] == 1
    with pytest.raises(FrozenInstanceError):
        a.sha = sha(3)
    changed = replace(b, files=(change(la=2),))
    assert compute(snapshot(a, changed))[-1].source_digest != outputs[-1].source_digest
    assert (
        compute(replace(original, repository_id="different"))[-1].source_digest
        != outputs[-1].source_digest
    )
    assert (
        compute(replace(original, history_coverage="recent_window"))[-1].source_digest
        != outputs[-1].source_digest
    )


def test_quality_quantiles_and_empty_missing_population():
    a = commit(1, files=[change(la=1)])
    b = commit(2, [1], 1, [change(la=3)])
    c = commit(3, [2], 2, [change(la=5)])
    vectors = compute(snapshot(a, b, c))
    report = quality(vectors)
    assert report["features"]["LA"] == {
        "valid": 3,
        "missing": 0,
        "missing_reasons": {},
        "min": 1,
        "p25": 2.0,
        "median": 3.0,
        "p75": 4.0,
        "p95": 4.8,
        "max": 5,
    }
    assert report["features"]["FIX"]["missing"] == 3
    assert report["features"]["FIX"]["median"] is None
    assert quality([])["features"]["LA"]["min"] is None
    assert report["source_digest"] == vectors[0].source_digest
    for population in ([vectors[0], vectors[0]], [vectors[0], compute(snapshot(a))[0]]):
        with pytest.raises(FeatureInputError, match="FEATURE_QUALITY_POPULATION_INVALID"):
            quality(population)


@pytest.mark.parametrize("bad", [None, {}, {"commit": {}}, {"commit": [], "identity": {}}])
def test_malformed_adapter_records_fail_safely(bad):
    with pytest.raises(FeatureInputError, match="FEATURE_RECORD_INVALID"):
        freeze_record(bad)


@pytest.mark.parametrize("bad", [None, "email@example.invalid", 1, []])
def test_raw_or_invalid_identity_is_rejected(bad):
    with pytest.raises(FeatureInputError, match="FEATURE_COMMIT_INVALID"):
        commit(1, author=bad)


@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan"), True])
def test_invalid_vector_values_are_rejected_before_quality(bad):
    value = compute(snapshot(commit(1)))[0]
    changed = tuple((key, bad if key == "LA" else v) for key, v in value.values)
    with pytest.raises(FeatureInputError, match="FEATURE_VALUE_INVALID"):
        replace(value, values=changed)


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_symlink_and_gitlink_are_unknown_even_with_parsed_counts(mode):
    vector = result(commit(1, files=[change(old_mode=mode, new_mode=mode)]))
    assert vector["values"]["LT"] is None and vector["values"]["LA"] is None


def test_actual_git_symlink_mode_and_blob_binding(tmp_path):
    root, _, fixed, shas = szz_history(tmp_path / "source")
    blob = git("-C", str(root), "hash-object", "-w", "--stdin", data=b"code.py").decode().strip()
    git("-C", str(root), "update-index", "--add", "--cacheinfo", "120000", blob, "link.py")
    git("-C", str(root), "commit", "-m", "Add symlink without Windows symlink privilege")
    head = git("-C", str(root), "rev-parse", "HEAD").decode().strip()
    reader = reader_for(root, tmp_path / "attempt")
    record = reader.record(head)
    frozen = freeze_native_record(reader, record)
    assert frozen.files[0].new_mode == "120000"
    inputs = tuple(freeze_native_record(reader, reader.record(s)) for s in shas) + (frozen,)
    value = compute(Snapshot("git-mode", head, inputs))[-1].as_dict()
    assert value["values"]["LT"] is None
    assert value["reasons"]["LT"] == "current_content_unsupported"
    record["files"][0]["new_blob"] = "f" * 40
    with pytest.raises(FeatureInputError, match="FEATURE_BLOB_CONFLICT"):
        freeze_native_record(reader, record)
    trusted = reader.record(fixed)
    trusted["commit"]["parser_version"] = "unsupported"
    with pytest.raises(FeatureInputError, match="FEATURE_VERSION_CONFLICT"):
        freeze_native_record(reader, trusted)
