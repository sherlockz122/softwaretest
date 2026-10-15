"""Independent coordinates, budgets and no-test/no-label boundaries, with inert real Git."""

import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import public_assets as assets
from packages.mining.research_dataset import CSV_FIELDS, PARTITIONS
from packages.semantic import admission as a
from packages.semantic import artifacts, policy
from packages.semantic.admission_review import license_evidence, review_bundle
from packages.semantic.git_inputs import extract
from packages.semantic.policy import SemanticError
from tests.test_semantic_kernel import git_fixture


@pytest.fixture
def work():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="admission-") as name:
        yield Path(name)


def test_hunk_coordinates_and_crlf_original_text():
    before = "first\r\nold\r\nlast\r\n"
    after = "first\r\nnew\r\nlast\r\n"
    groups, hunks = a.hunk_groups([(before, after)])
    assert hunks == [{"file": 0, "id": 0, "old": [1, 2], "new": [1, 2], "tag": "replace"}]
    added, audit = a.select_lines(groups["added"], list)
    deleted, _ = a.select_lines(groups["deleted"], list)
    base, _ = a.select_lines(groups["base"], list)
    assert added == "new\r\n" and deleted == "old\r\n" and base == before
    assert audit["selected_coordinates"] == [{"file": 0, "line": 2}]


@pytest.mark.parametrize(
    "separator", ["\u2028", "\r", "\x0c"], ids=["unicode-separator", "bare-cr", "form-feed"]
)
def test_coordinates_follow_git_lf_not_unicode_logical_lines(separator):
    groups, hunks = a.hunk_groups(
        [(f"alpha{separator}old\nstable\n", f"alpha{separator}new\nstable\n")]
    )
    assert hunks[0]["old"] == [0, 1] and hunks[0]["new"] == [0, 1]
    value, audit = a.select_lines(groups["added"], list)
    assert value == f"alpha{separator}new\n" and audit["selected_coordinates"] == [
        {"file": 0, "line": 1}
    ]


def test_round_robin_does_not_let_first_hunk_take_every_line(monkeypatch):
    monkeypatch.setattr(a, "CONTENT_TOKENS", 6)
    groups = [
        {"hunk": 0, "eligible_lines": 2, "candidates": [(0, 0, "a\n"), (0, 1, "b\n")]},
        {"hunk": 1, "eligible_lines": 2, "candidates": [(1, 0, "c\n"), (1, 1, "d\n")]},
    ]
    value, audit = a.select_lines(groups, list)
    assert value == "a\n\nc\n" and audit["represented_hunks"] == 2
    assert audit["selected_tokens"] == 5


def test_long_line_omitted_explicitly_and_utf8_budget(monkeypatch):
    monkeypatch.setattr(a, "CONTENT_TOKENS", 8)
    groups = [{"hunk": 0, "eligible_lines": 2, "candidates": [(0, 0, "x" * 100), (0, 1, "中文\n")]}]
    value, audit = a.select_lines(groups, lambda s: list(s.encode()))
    assert value == "中文\n" and audit["selected_tokens"] == 7
    assert audit["candidate_lines_before_cap"] == 2 and len(audit["selected_coordinates"]) == 1


def test_hunk_cap_is_auditable_not_full_coverage(monkeypatch):
    monkeypatch.setattr(a, "MAX_HUNKS", 1)
    groups, hunks = a.hunk_groups([("a\nx\nb\ny\nc\n", "A\nx\nB\ny\nC\n")])
    assert len(hunks) == 3 and len(groups["added"]) == 1


def test_test_row_is_refused_before_touching_git():
    with pytest.raises(SemanticError, match="OLD_TEST_FORBIDDEN"):
        a.transform({"member": {"partition": "test"}}, None, list)


def test_real_git_contexts_keep_population_and_original_artifacts(work):
    dataset, source, _ = git_fixture(work)
    original = extract(dataset, source, work / "v1")
    before = artifacts.verify(original)
    safe_root = work / "safe"
    safe_root.mkdir()
    attempt = safe_root / ".inert"
    attempt.mkdir()
    (attempt / "inert").write_bytes(b"not a model, test encoder injected")
    safe = artifacts.publish(
        attempt,
        safe_root,
        {
            "safe_format": "safetensors",
            "weights_only": True,
            "original": {"revision": policy.REVISION},
        },
        ["inert"],
    )
    result = a.contexts(original, source, safe, work / "v2", encode=list)
    manifest = artifacts.verify(result)
    rows = list(artifacts.records(result))
    assert manifest["accepted"] == 8 and {r["member"]["partition"] for r in rows} == {
        "train",
        "validation",
    }
    assert all(r["context"]["full_commit_encoded"] is False for r in rows)
    assert all(len(r["roles"][role]) <= 252 for r in rows for role in ["added", "deleted", "base"])
    assert artifacts.verify(original) == before
    assert not manifest["supports"]["calibration_and_threshold_floor_met"]
    row = rows[0]
    row["files"][0]["before_bytes"] += 1
    with TemporaryDirectory(dir=work) as home:
        reader, _ = a.source_reader(source, Path(home))
        with pytest.raises(SemanticError, match="BLOB_LENGTH_DRIFT"):
            a.transform(row, reader, list)


def test_reservation_fixed_boundary_exclusion_and_label_independence():
    rows = [
        {"commit_id": f"{i:040x}", "author_epoch": 50, "committer_epoch": i, "parents": ["f" * 40]}
        for i in range(10)
    ]
    rows[5]["parents"] = ["a" * 40, "b" * 40]
    excluded = {(policy.REPOSITORY, f"{3:040x}")}
    result = a.choose_reservation(rows, excluded, start=2, stop=7, limit=2)
    assert [r["commit_id"] for r in result] == [f"{2:040x}", f"{4:040x}"]
    altered = [r | {"label": 1, "score": -999} for r in rows]
    assert [
        r["commit_id"] for r in a.choose_reservation(altered, excluded, start=2, stop=7, limit=2)
    ] == [r["commit_id"] for r in result]


@pytest.mark.parametrize("limit", [True, 0, 65])
def test_reservation_limit(limit):
    with pytest.raises(SemanticError, match="RESERVATION_LIMIT"):
        a.choose_reservation([], set(), limit=limit)


def test_commit_clock_oracle_and_hash_drift():
    raw = (
        b"tree "
        + b"a" * 40
        + b"\nparent "
        + b"b" * 40
        + b"\nauthor A <a@invalid> 123 +0800\ncommitter B <b@invalid> 456 +0000\n\nmessage\n"
    )
    digest = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    value = a.commit_clocks(raw, digest)
    assert value["author_epoch"] == 123 and value["committer_epoch"] == 456
    with pytest.raises(SemanticError, match="COMMIT_DRIFT"):
        a.commit_clocks(raw + b"changed", digest)


def test_native_reservation_is_outside_frozen_members_and_unlabeled(work, monkeypatch):
    dataset, source, rows = git_fixture(work)
    subset = work / "excluded"
    subset.mkdir()
    files = {}
    for i, part in enumerate(PARTITIONS):
        path = subset / (part + ".csv")
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows[i * 4 : (i + 1) * 4 - (1 if part == "test" else 0)])
        files[part] = artifacts.file_digest(path)
    assets.write_json(subset / "manifest.json", assets.sealed({"files": files}))
    monkeypatch.setattr(a, "START", 1000000000)
    monkeypatch.setattr(a, "STOP", 1000020000)
    result = a.reserve(subset, source, work / "reservation")
    manifest = artifacts.verify(result)
    reserved = json.loads((result / "reservation.json").read_text())["records"]
    assert manifest["reserved"] == 1 and reserved[0]["commit_id"] == rows[-1]["commit_id"]
    assert (
        not manifest["holdout_ready"]
        and "label" not in reserved[0]
        and "message" not in reserved[0]
    )
    with pytest.raises(SemanticError, match="NO_FUTURE_CANDIDATES"):
        a.reserve(dataset, source, work / "empty")
    assert not list((work / "empty").glob("*/manifest.json"))


def license_fixture(work):
    path = work / "license.json"
    assets.write_json(
        path,
        assets.sealed(
            {
                "version": "semantic-license-observation-v1",
                "model": policy.MODEL,
                "revision": policy.REVISION,
                "redistribution_approved": False,
            }
        ),
    )
    return path


def test_review_export_is_blank_and_cannot_mutate_old_state(work, monkeypatch):
    from packages.semantic import admission_review as ar

    store = work / "reviews"
    cases = ["a" * 64, "b" * 64]
    for case in cases:
        (store / "cases" / case).mkdir(parents=True)
    monkeypatch.setattr(ar.reviews, "load_initial", lambda p: {"binding": {"inert": True}})
    original = {
        k: [] if k == "display_links" else False if k == "display_links_truncated" else "unknown"
        for k in [
            "project",
            "commit_id",
            "display_links",
            "display_links_truncated",
            "raw_links_digest",
            "negative_coverage",
        ]
    }
    original["published_label"] = "hidden"
    monkeypatch.setattr(
        ar.reviews,
        "case_history",
        lambda p, c: {
            "records": [
                {"packet": original},
                {"revision": 2, "status": "unresolved", "content_hash": "f" * 64, "evidence": []},
            ]
        },
    )
    result = review_bundle(store, license_fixture(work), work / "bundles")
    manifest = artifacts.verify(result)
    assert manifest["independent_reviews_completed"] == 0 and not manifest["old_reviews_mutated"]
    packet = json.loads((result / "packet.json").read_text())
    assert all("published_label" not in p["hypothesis"] for p in packet["cases"])
    for name in ["reviewer-A.json", "reviewer-B.json"]:
        form = json.loads((result / name).read_text())
        assert (
            not form["completed"]
            and form["reviewer_id"] is None
            and all(d["conclusion"] is None for d in form["decisions"])
        )
    assert original["published_label"] == "hidden"


def test_license_metadata_cannot_auto_authorize_distribution(work):
    path = license_fixture(work)
    assert license_evidence(path)["redistribution_approved"] is False
    other = work / "claimed.json"
    assets.write_json(
        other,
        assets.sealed(
            {
                "version": "semantic-license-observation-v1",
                "model": policy.MODEL,
                "revision": policy.REVISION,
                "redistribution_approved": True,
            }
        ),
    )
    with pytest.raises(SemanticError, match="REQUIRES_AUTHORITY"):
        license_evidence(other)


def test_support_counts_keep_same_timestamp_groups_and_sparse_floor():
    rows = [{"member": {"partition": "train", "label": "1", "event_epoch": "0"}}]
    for epoch, label in [(1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1), (4, 0), (4, 1)]:
        rows.append(
            {"member": {"partition": "validation", "label": str(label), "event_epoch": str(epoch)}}
        )
    audit = a.support_audit(rows)
    assert audit["counts"]["calibration"] == {"0": 2, "1": 2}
    assert audit["counts"]["threshold"] == {"0": 2, "1": 2}
    assert not audit["calibration_and_threshold_floor_met"] and not audit["resplit_performed"]
    with pytest.raises(SemanticError, match="CALIBRATION_UNUSABLE"):
        a.support_audit(rows[:3])


def test_isolated_reader_denies_network_commands_and_enforces_output(work, monkeypatch):
    import time

    from packages.semantic.admission_git import NativeReader, native_environment

    dataset, source, _ = git_fixture(work)
    monkeypatch.setenv("DG_MYSQL_PASSWORD", "must-not-be-copied")
    home = work / "read-home"
    home.mkdir()
    env = native_environment(home, source / "source.git")
    assert "DG_MYSQL_PASSWORD" not in env and env["GIT_NO_LAZY_FETCH"] == "1"
    config = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert config["protocol.allow"] == "never" and config["safe.directory"] == str(
        source / "source.git"
    )
    reader = NativeReader(source / "source.git", home, env, lambda: None, time.monotonic() + 30)
    with pytest.raises(SemanticError, match="COMMAND_FORBIDDEN"):
        reader.command(["fetch", "anything"])
    with pytest.raises(SemanticError, match="OUTPUT_LIMIT"):
        reader.command(["rev-list", "--all"], limit=1)
    reader.deadline = 0
    with pytest.raises(SemanticError, match="DEADLINE"):
        reader.command(["rev-parse", "HEAD"])
    assert not (home / "read.out").exists() and not (home / "read.err").exists()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_native_reader_rechecks_terminal_output_burst(work, monkeypatch, stream):
    import time

    from packages.semantic import admission_git as native

    home, bare = work / "terminal-reader", work / "terminal.git"
    home.mkdir()
    bare.mkdir()

    class TerminalBurst:
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.sink = kwargs[stream]
            self.finished = False

        def poll(self):
            if not self.finished:
                self.sink.write(b"x" * (2 if stream == "stdout" else 65537))
                self.sink.flush()
                self.finished = True
            return 0

    monkeypatch.setattr(native.subprocess, "Popen", TerminalBurst)
    reader = native.NativeReader(bare, home, {}, lambda: None, time.monotonic() + 30)
    with pytest.raises(SemanticError, match="OUTPUT_LIMIT"):
        reader.command(["rev-parse", "HEAD"], limit=1)
    assert not (home / "read.out").exists() and not (home / "read.err").exists()


@pytest.mark.parametrize("wrong_members", [False, True])
def test_readiness_is_bound_to_actual_cache_and_never_authorizes(work, wrong_members):
    import numpy as np

    from packages.semantic.admission_review import readiness

    root = work / "artifacts"
    root.mkdir()

    def publish(name, metadata, write):
        stage = root / ("." + name)
        stage.mkdir()
        write(stage)
        return artifacts.publish(stage, root, metadata, [p.name for p in stage.iterdir()])

    rows = [
        {"member": {"partition": part, "commit_id": f"{i:040x}"}, "text_hash": f"{i:064x}"}
        for i, part in enumerate(["train", "validation"])
    ]

    def context_files(stage):
        (stage / "records.jsonl").write_text("".join(json.dumps(v) + "\n" for v in rows))
        assets.write_json(
            stage / "audit.json",
            {
                "hunks_beyond_cap": 0,
                "cross_split_exact_duplicate_groups": 0,
                "roles": {"base": {"unrepresented_hunks": 1}},
            },
        )

    contexts = publish(
        "contexts",
        {
            "version": "hunk-context-v2",
            "accepted": 2,
            "checkpoint_manifest": "a" * 64,
            "supports": {"calibration_and_threshold_floor_met": False},
        },
        context_files,
    )

    def cache_files(stage):
        np.save(stage / "embeddings.npy", np.zeros((2, 3076), dtype=np.float32), allow_pickle=False)
        assets.write_json(
            stage / "rows.json", {"records": list(reversed(rows)) if wrong_members else rows}
        )
        assets.write_json(
            stage / "coverage.json",
            {
                "records": [
                    {
                        "identity": r["member"]["commit_id"],
                        "roles": {role: {"omitted_tokens": 0} for role in policy.ROLES},
                    }
                    for r in rows
                ]
            },
        )

    cache = publish(
        "cache",
        {
            "shape": [2, 3076],
            "binding": {
                "inputs": artifacts.verify(contexts)["content_hash"],
                "checkpoint": "a" * 64,
            },
        },
        cache_files,
    )
    reservation = publish(
        "reservation",
        {"version": "unlabeled-future-reservation-v1", "holdout_ready": False},
        lambda p: (p / "value").write_bytes(b"unlabeled"),
    )
    license_path = license_fixture(work)
    license_hash = license_evidence(license_path)["content_hash"]
    bundle = publish(
        "review",
        {"license_observation": license_hash, "independent_reviews_completed": 0},
        lambda p: (p / "value").write_bytes(b"blank"),
    )
    if wrong_members:
        with pytest.raises(SemanticError, match="CACHE_CONTENT_DRIFT"):
            readiness(contexts, reservation, cache, bundle, license_path, work / "refused")
        assert not (work / "refused").exists()
    else:
        target = readiness(contexts, reservation, cache, bundle, license_path, work / "reports")
        report = json.loads((target / "report.json").read_text())
        assert (
            not report["candidate_release"]
            and not report["all_historical_or_potential_issues_guaranteed_resolvable"]
        )
        assert report["checks"]["selected_contexts_fully_encoded"]
        assert not report["checks"]["candidate_hunk_context_represented"]
        assert not report["checks"]["calibration_and_threshold_minimum_floor"]
        assert report["new_test_scores_or_classifier_fits"] == 0
