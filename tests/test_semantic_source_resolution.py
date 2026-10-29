"""Real Git identities, cache refusals and separately versioned parent evidence."""

import copy
import hashlib
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from packages.mining import public_assets as assets
from packages.mining import szz_engine
from packages.repositories.safety import RepositoryError
from packages.semantic import source_resolution as resolution
from packages.semantic.git_inputs import native_environment
from packages.semantic.policy import SemanticError


@pytest.fixture
def fixture():
    parent = assets.safe_path(assets.ROOT / "runtime/temp")
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="source-resolution-") as name:
        directory = assets.safe_path(Path(name))
        assert directory.resolve().is_relative_to(parent.resolve())
        bare = directory / "source.git"
        git = shutil.which("git")
        assert git
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Synthetic Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2023-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2023-01-01T00:00:00+00:00",
        }

        def command(*arguments, raw=None):
            return (
                subprocess.run(
                    [git, "-C", str(directory), *arguments],
                    input=raw,
                    capture_output=True,
                    env=environment,
                    check=True,
                    timeout=10,
                )
                .stdout.strip()
                .decode("ascii")
            )

        command("init", "--quiet", "--bare", str(bare))

        def objects(*arguments, raw=None):
            return command("--git-dir=" + str(bare), *arguments, raw=raw)

        blob = objects("hash-object", "-w", "--stdin", raw=b"hello\n")
        tree = objects("mktree", raw=f"100644 blob {blob}\tcode.py\n".encode())
        empty = objects("mktree", raw=b"")
        foundation = objects("commit-tree", empty, "-m", "foundation")
        root = objects("commit-tree", tree, "-p", foundation, "-m", "introduce code")
        changed_blob = objects("hash-object", "-w", "--stdin", raw=b"world\n")
        changed_tree = objects("mktree", raw=f"100644 blob {changed_blob}\tcode.py\n".encode())
        child = objects("commit-tree", changed_tree, "-p", root, "-m", "fix error")
        objects("update-ref", "refs/heads/fixture", child)
        token = hashlib.sha256((root + child + blob).encode()).hexdigest()

        def reader(label, cache=True, check=lambda: None):
            attempt = directory / label
            attempt.mkdir()
            return resolution.Reader(
                bare,
                attempt,
                native_environment(attempt, bare),
                check,
                time.monotonic() + 30,
                source_id=token,
                cache=cache,
            )

        yield {
            "directory": directory,
            "bare": bare,
            "root": root,
            "child": child,
            "blob": blob,
            "tree": tree,
            "changed_tree": changed_tree,
            "objects": objects,
            "reader": reader,
        }


def test_real_blob_and_parent_pair_cache_equal_uncached_reference(fixture):
    cached, reference = fixture["reader"]("cached"), fixture["reader"]("reference", cache=False)
    for _ in range(3):
        assert (
            cached.blob(fixture["blob"], "100644")
            == reference.blob(fixture["blob"], "100644")
            == b"hello\n"
        )
        assert (
            cached.ancestor(fixture["root"], fixture["child"])
            is reference.ancestor(fixture["root"], fixture["child"])
            is True
        )
        assert (
            cached.ancestor(fixture["child"], fixture["root"])
            is reference.ancestor(fixture["child"], fixture["root"])
            is False
        )
    assert cached.statistics["command_attempts"] == 6
    assert reference.statistics["command_attempts"] == 18
    assert cached.statistics["blob_hits"] == 2
    assert cached.statistics["ancestor_hits"] == 4


@pytest.mark.parametrize("operation", ["blob", "ancestor"])
def test_cache_hit_still_checks_cancellation_and_deadline(fixture, operation):
    cancelled = False

    def check():
        if cancelled:
            raise ValueError("fixture cancellation")

    reader = fixture["reader"]("checked", check=check)

    def call():
        return (
            reader.blob(fixture["blob"], "100644")
            if operation == "blob"
            else reader.ancestor(fixture["root"], fixture["child"])
        )

    call()
    cancelled = True
    with pytest.raises(ValueError, match="fixture cancellation"):
        call()
    cancelled = False
    reader.deadline = time.monotonic() - 1
    with pytest.raises(RepositoryError) as error:
        call()
    assert error.value.code == "REPOSITORY_PARSE_TIMEOUT"


@pytest.mark.parametrize("drift", ["source", "environment", "identity", "cache_mode"])
def test_binding_drift_refused_before_a_cached_value(fixture, drift):
    reader = fixture["reader"]("drift")
    reader.blob(fixture["blob"], "100644")
    attempts = reader.statistics["command_attempts"]
    if drift == "source":
        reader.bare = fixture["directory"] / "different.git"
    elif drift == "environment":
        reader.environment["GIT_CONFIG_NOSYSTEM"] = "0"
    elif drift == "identity":
        reader.source_id = "f" * 64
    else:
        reader.cached = False
    with pytest.raises(SemanticError, match="SOURCE_RUNTIME_BINDING_CHANGED"):
        reader.blob(fixture["blob"], "100644")
    assert reader.statistics["command_attempts"] == attempts


def test_blob_modes_eviction_and_oversize_none_are_not_conflated(fixture, monkeypatch):
    monkeypatch.setattr(resolution, "BLOB_CACHE_BYTES", 8)
    monkeypatch.setattr(resolution, "BLOB_CACHE_ENTRIES", 1)
    reader = fixture["reader"]("bounded")
    for mode in ("100644", "100755", "100644"):
        assert reader.blob(fixture["blob"], mode) == b"hello\n"
        assert reader.blob_bytes <= 8 and len(reader.blobs) <= 1
    assert reader.statistics["command_attempts"] == 6
    oversized = fixture["objects"]("hash-object", "-w", "--stdin", raw=b"x" * (1024**2 + 1))
    before = reader.statistics["command_attempts"]
    assert reader.blob(oversized, "100644") is None
    assert reader.blob(oversized, "100644") is None
    assert reader.statistics["command_attempts"] == before + 2


def test_ancestry_errors_not_cached_and_pair_eviction(fixture, monkeypatch):
    monkeypatch.setattr(resolution, "ANCESTRY_CACHE_ENTRIES", 1)
    reader = fixture["reader"]("ancestry")
    assert reader.ancestor(fixture["root"], fixture["child"])
    assert not reader.ancestor(fixture["child"], fixture["root"])
    assert reader.ancestor(fixture["root"], fixture["child"])
    assert len(reader.ancestry) == 1 and reader.statistics["command_attempts"] == 6
    original = reader.command

    def failed(arguments, **options):
        if arguments[0] == "merge-base":
            raise RepositoryError(422, "REPOSITORY_PARSE_FAILED")
        return original(arguments, **options)

    monkeypatch.setattr(reader, "command", failed)
    for _ in range(2):
        with pytest.raises(RepositoryError):
            reader.ancestor(fixture["child"], fixture["root"])
        assert (fixture["child"], fixture["root"]) not in reader.ancestry


def test_corrupt_object_response_never_enters_blob_cache(fixture, monkeypatch):
    reader = fixture["reader"]("hash")
    original = resolution.native_runtime.run

    def corrupted(arguments, **options):
        result = original(arguments, **options)
        return (
            replace(result, data=b"corrupt\n")
            if arguments[-3:-1] == ["cat-file", "blob"]
            else result
        )

    monkeypatch.setattr(resolution.native_runtime, "run", corrupted)
    with pytest.raises(SemanticError, match="SOURCE_OBJECT_DRIFT"):
        reader.blob(fixture["blob"], "100644")
    assert not reader.blobs and reader.blob_bytes == 0


def test_full_real_native_record_and_szz_equal_legacy_reference(fixture):
    candidate = fixture["reader"]("record")
    attempt = fixture["directory"] / "legacy-reference"
    attempt.mkdir()
    reference = resolution.original.Reader(
        fixture["bare"],
        attempt,
        native_environment(attempt, fixture["bare"]),
        lambda: None,
        time.monotonic() + 30,
    )
    actual, expected = candidate.record(fixture["child"]), reference.record(fixture["child"])
    assert actual == expected
    eligible = {fixture["root"]: expected["commit"]["committer_time"]}
    source = {"visible": True, "candidate": True, "available_at": "2023-01-08T00:00:00+00:00"}
    left = szz_engine.trace(candidate, source, actual["commit"], actual["files"], eligible)
    right = szz_engine.trace(reference, source, expected["commit"], expected["files"], eligible)
    assert left == right
    assert len(left["links"]) == 1
    assert left["links"][0]["blamed_sha"] == fixture["root"]
    assert left["links"][0]["line_hash"] == hashlib.sha256(b"hello").hexdigest()
    assert left["links"][0]["eligible"] is True


def actual_edges(fixture, *, disagree=False, binary=False):
    reader = fixture["reader"]("edges")
    objects = fixture["objects"]
    first = objects("commit-tree", fixture["tree"], "-p", fixture["root"], "-m", "branch A")
    second = (
        fixture["child"]
        if disagree
        else objects("commit-tree", fixture["tree"], "-p", fixture["root"], "-m", "branch B")
    )
    tree = fixture["changed_tree"]
    if binary:
        blob = objects("hash-object", "-w", "--stdin", raw=b"\0opaque")
        tree = objects("mktree", raw=f"100644 blob {blob}\tcode.py\n".encode())
    merged = objects("commit-tree", tree, "-p", first, "-p", second, "-m", "fix error")
    metadata, _ = resolution.original._metadata(reader, merged)
    root_metadata, _ = resolution.original._metadata(reader, fixture["root"])
    target_record = reader.record(fixture["root"])
    assert target_record["commit"]["parse_status"] == "parsed"
    target = {
        "metadata": root_metadata,
        "parse_status": "parsed",
        "files": [
            {k: v for k, v in f.items() if k not in {"diff_text", "line_numbers"}}
            for f in target_record["files"]
        ],
    }
    declaration = {
        "targets": [root_metadata | {"role": "calibration_candidate"}],
        "observations": [metadata],
        "reachable_metadata": {fixture["root"]: root_metadata},
    }
    edges = []
    for parent in metadata["parents"]:
        files = reader.files(merged, [parent])
        commit = {
            "sha": merged,
            "parents": [parent],
            "parse_status": "parsed",
            "committer_time": datetime.fromtimestamp(metadata["committer_epoch"], UTC).replace(
                tzinfo=None
            ),
        }
        trace = szz_engine.trace(
            reader,
            {"visible": True, "candidate": True, "available_at": "2023-01-08T00:00:00+00:00"},
            commit,
            files,
            {fixture["root"]: commit["committer_time"]},
        )
        positive = sorted({link["blamed_sha"] for link in trace["links"] if link["eligible"]})
        assert all(link["eligible"] for link in trace["links"])
        value = {
            "metadata": metadata | {"parents": [parent]},
            "trace": trace,
            "positive_targets": positive,
            "target_gaps": [],
            "global_gaps": trace["reasons"]
            if any(f["status"] == "unknown" for f in trace["files"])
            else [],
            "outside_frame": [],
            "target_evidence": {identity: target for identity in positive},
        }
        edges.append({"parent_sha": parent, "observation": value})
    return metadata, edges, declaration


def test_real_merge_consensus_has_every_parent_line_witness(fixture):
    metadata, edges, declaration = actual_edges(fixture)
    result = resolution.combine_edges(metadata, edges, declaration)
    assert result["metadata"]["parents"] == metadata["parents"] and len(result["edges"]) == 2
    assert result["positive_targets"] == [fixture["root"]]
    assert not result["target_gaps"] and not result["global_gaps"]
    resolution.verify_resolution(result, declaration)
    rows = resolution.original.outcomes(declaration, [result])
    assert rows[0]["provisional_label"] == 1 and rows[0]["actual_label_available_at"] is None
    result["metadata"]["parents"].clear()
    assert len(metadata["parents"]) == 2


def test_real_merge_disagreement_is_target_unknown_not_clean(fixture):
    metadata, edges, declaration = actual_edges(fixture, disagree=True)
    result = resolution.combine_edges(metadata, edges, declaration)
    assert result["positive_targets"] == [] and result["target_gaps"] == [fixture["root"]]
    assert result["global_gaps"] == []
    assert resolution.original.outcomes(declaration, [result])[0]["provisional_label"] is None


def test_real_unmapped_binary_parent_edges_remain_global_unknown(fixture):
    metadata, edges, declaration = actual_edges(fixture, binary=True)
    result = resolution.combine_edges(metadata, edges, declaration)
    assert result["positive_targets"] == [] and result["global_gaps"] == ["binary"]
    assert resolution.original.outcomes(declaration, [result])[0]["provisional_label"] is None


@pytest.mark.parametrize("tampering", ["missing", "duplicate", "order", "outcome"])
def test_parent_edge_or_derived_outcome_tampering_refused(fixture, tampering):
    metadata, edges, declaration = actual_edges(fixture)
    result = resolution.combine_edges(metadata, edges, declaration)
    forged = copy.deepcopy(result)
    if tampering == "missing":
        forged["edges"].pop()
    elif tampering == "duplicate":
        forged["edges"][1] = forged["edges"][0]
    elif tampering == "order":
        forged["edges"].reverse()
    else:
        forged["positive_targets"] = []
    with pytest.raises(SemanticError):
        resolution.verify_resolution(forged, declaration)


@pytest.mark.parametrize("code", ["NATIVE_PROCESS_CLEANUP_FAILED", "NATIVE_STREAM_CLEANUP_FAILED"])
def test_unsafe_runtime_failure_cannot_be_swallowed_as_parse_gap(fixture, monkeypatch, code):
    reader = fixture["reader"]("unsafe-runtime")

    def fail(*args, **kwargs):
        raise RepositoryError(422, code)

    monkeypatch.setattr(resolution.native_runtime, "run", fail)
    with pytest.raises(SemanticError, match=code):
        reader.record(fixture["child"])
    assert not reader.blobs and not reader.ancestry


def context_fixture(fixture, label, *, cache=None):
    reader = fixture["reader"](label)
    metadata = {
        identity: resolution.original._metadata(reader, identity)[0]
        for identity in (fixture["root"], fixture["child"])
    }
    directory = cache or fixture["directory"] / (label + "-cache")
    directory.mkdir(exist_ok=True)
    declaration = {
        "targets": [metadata[fixture["root"]] | {"role": "calibration_candidate"}],
        "observations": list(metadata.values()),
        "reachable_metadata": metadata,
    }
    return resolution.Context(
        declaration,
        fixture["bare"].parent,
        directory,
        reader,
        lambda: None,
        binding="e" * 64,
        execution={"sources": {"synthetic": "a" * 64}},
        execution_check=lambda: {"sources": {"synthetic": "a" * 64}},
    )


@pytest.mark.parametrize("fatal", [False, True], ids=["success", "fatal"])
def test_nested_target_preserves_parent_remaining_active_budget(fixture, monkeypatch, fatal):
    context = context_fixture(fixture, "nested-active-budget")
    clock = [10000.0]
    context.reader.deadline = 10020.0
    returned = {"owned": [1]}

    def target_operation(self, sha):
        assert sha == fixture["root"]
        self.reader.deadline = clock[0] + 30
        clock[0] += 7
        if fatal:
            raise SemanticError("NATIVE_PROCESS_CLEANUP_FAILED")
        return returned

    monkeypatch.setattr(resolution, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(resolution.original.Context, "target", target_operation)
    if fatal:
        with pytest.raises(SemanticError, match="NATIVE_PROCESS_CLEANUP_FAILED"):
            context.target(fixture["root"])
        assert not list(context.cache.glob("observation-*.json"))
        assert not list(context.cache.glob("target-*.json"))
    else:
        actual = context.target(fixture["root"])
        assert actual == returned and actual is not returned
        assert actual["owned"] is not returned["owned"]
    assert context.reader.deadline == 10027.0
    assert context.reader.deadline - clock[0] == 20.0


def test_real_target_timeout_cold_and_warm_preserve_same_two_file_trace(fixture, monkeypatch):
    objects = fixture["objects"]
    a_before = objects("hash-object", "-w", "--stdin", raw=b"a_value = 1\n")
    b_before = objects("hash-object", "-w", "--stdin", raw=b"b_value = 2\n")
    before = objects(
        "mktree",
        raw=f"100644 blob {a_before}\ta.py\n100644 blob {b_before}\tb.py\n".encode(),
    )
    root = objects("commit-tree", before, "-p", fixture["root"], "-m", "introduce two files")
    a_after = objects("hash-object", "-w", "--stdin", raw=b"a_value = 3\n")
    b_after = objects("hash-object", "-w", "--stdin", raw=b"b_value = 4\n")
    after = objects(
        "mktree", raw=f"100644 blob {a_after}\ta.py\n100644 blob {b_after}\tb.py\n".encode()
    )
    fixed = objects("commit-tree", after, "-p", root, "-m", "fix error in two files")

    def context(label, cache):
        reader = fixture["reader"](label)
        metadata = {
            sha: resolution.original._metadata(reader, sha)[0]
            for sha in (fixture["root"], root, fixed)
        }
        declaration = {
            "targets": [metadata[root] | {"role": "calibration_candidate"}],
            "observations": [metadata[fixed]],
            "reachable_metadata": metadata,
        }
        cache.mkdir(exist_ok=True)
        return resolution.Context(
            declaration,
            fixture["bare"].parent,
            cache,
            reader,
            lambda: None,
            binding="d" * 64,
            execution={"synthetic": "a" * 64},
            execution_check=lambda: {"synthetic": "a" * 64},
        )

    cold = context("cold-target-operation", fixture["directory"] / "cold-operation-cache")

    def expired_target_record(sha):
        assert sha == root
        # Reproduce the expired reader state left by a bounded target parse;
        # the surrounding two-file trace still performs actual Git reads.
        cold.reader.deadline = time.monotonic() - 1
        raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")

    monkeypatch.setattr(cold.reader, "record", expired_target_record)
    expected = cold.declaration["observations"][0]
    cold_value = cold.observation(expected)
    warm_cache = fixture["directory"] / "warm-operation-cache"
    warm_cache.mkdir()
    for path in [cold.cache / "cache.json", *cold.cache.glob("target-*.json")]:
        shutil.copy2(path, warm_cache / path.name)
    warm = context("warm-target-operation", warm_cache)

    def unexpected_target_record(sha):
        pytest.fail("warm immutable target must be reused without another native parse")

    monkeypatch.setattr(warm.reader, "record", unexpected_target_record)
    warm_value = warm.observation(expected)
    assert cold_value == warm_value
    result = cold_value["resolution"]
    assert result["global_gaps"] == [] and result["target_gaps"] == [root]
    links = result["edges"][0]["observation"]["trace"]["links"]
    assert len(links) == 2 and {link["path"] for link in links} == {"a.py", "b.py"}
    assert all(link["blamed_sha"] == root and not link["eligible"] for link in links)
    resolution.verify_unit(cold_value, cold.declaration)


def test_complete_context_preserves_parent_files_target_parse_and_resumes(fixture):
    context = context_fixture(fixture, "context")
    expected = context.declaration["observations"][1]
    value = context.observation(expected)
    assert value["resolution"]["positive_targets"] == [fixture["root"]]
    assert value["resolution"]["metadata"] == expected
    assert value["assessment"]["rule_candidate"] is True
    edge = value["resolution"]["edges"][0]
    assert edge["files"][0]["old_blob"] == fixture["blob"]
    assert edge["files"][0]["line_numbers"]["deleted"] == [1]
    assert len(edge["files"][0]["diff_hash"]) == 64
    resolution.verify_unit(value, context.declaration)
    before = dict(context.reader.statistics)
    assert context.observation(expected) == value
    assert dict(context.reader.statistics) == before
    resumed = context_fixture(fixture, "resume", cache=context.cache)
    assert resumed.observation(expected) == value and resumed.created == 0
    assert not resumed.reader.statistics["blob_hits"]


@pytest.mark.parametrize("tampering", ["message", "assessment", "native_file", "metadata"])
def test_context_publication_rejects_inconsistent_exported_evidence(fixture, tampering):
    context = context_fixture(fixture, "verify")
    value = context.observation(context.declaration["observations"][1])
    forged = copy.deepcopy(value)
    if tampering == "message":
        forged["raw_commit_hex"] = forged["raw_commit_hex"][:-2] + "00"
    elif tampering == "assessment":
        forged["assessment"]["rule_candidate"] = False
    elif tampering == "native_file":
        forged["resolution"]["edges"][0]["files"][0]["old_blob"] = "a" * 40
    else:
        forged["resolution"]["metadata"]["parents"] = []
    with pytest.raises(SemanticError):
        resolution.verify_unit(forged, context.declaration)


def test_context_execution_drift_refuses_before_immutable_publication(fixture):
    context = context_fixture(fixture, "execution-drift")
    context.execution_check = lambda: {"sources": {"synthetic": "b" * 64}}
    with pytest.raises(SemanticError, match="SOURCE_EXECUTION_DRIFT"):
        context.observation(context.declaration["observations"][1])
    assert not list(context.cache.glob("observation-*.json"))


@pytest.mark.parametrize("disagree", [False, True])
def test_context_traces_complete_real_merge_without_changing_raw_parents(fixture, disagree):
    metadata, _, declaration = actual_edges(fixture, disagree=disagree)
    context = context_fixture(fixture, "complete-merge")
    context.declaration["observations"].append(metadata)
    context.declaration["reachable_metadata"][metadata["commit_id"]] = metadata
    context.expected[metadata["commit_id"]] = metadata
    value = context.observation(metadata)
    resolution.verify_unit(value, context.declaration)
    result = value["resolution"]
    assert result["metadata"]["parents"] == metadata["parents"]
    assert [edge["parent_sha"] for edge in result["edges"]] == metadata["parents"]
    assert result["positive_targets"] == ([] if disagree else [fixture["root"]])
    assert result["target_gaps"] == ([fixture["root"]] if disagree else [])
    assert not result["global_gaps"]
    assert declaration["targets"][0]["commit_id"] == fixture["root"]


def test_context_cannot_reuse_tampered_or_foreign_cache(fixture):
    context = context_fixture(fixture, "bound-cache")
    value = context.observation(context.declaration["observations"][1])
    value["resolution"]["positive_targets"].clear()
    assert context.observation(context.declaration["observations"][1])["resolution"][
        "positive_targets"
    ]
    path = next(context.cache.glob("observation-*.json"))
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(SemanticError, match="SOURCE_CACHE_DRIFT"):
        context_fixture(fixture, "tampered-cache", cache=context.cache)


def test_context_parse_timeout_keeps_explicit_gap_and_fatal_cleanup_aborts(fixture, monkeypatch):
    context = context_fixture(fixture, "timeout-gap")

    def unavailable(*args, **kwargs):
        raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")

    monkeypatch.setattr(context.reader, "files", unavailable)
    value = context.observation(context.declaration["observations"][1])
    assert value["resolution"]["global_gaps"] == ["REPOSITORY_PARSE_TIMEOUT"]
    assert not value["resolution"]["positive_targets"]
    resolution.verify_unit(value, context.declaration)

    other = context_fixture(fixture, "cleanup-fatal")

    def unsafe(*args, **kwargs):
        raise SemanticError("NATIVE_PROCESS_CLEANUP_FAILED")

    monkeypatch.setattr(other.reader, "files", unsafe)
    with pytest.raises(SemanticError, match="NATIVE_PROCESS_CLEANUP_FAILED"):
        other.observation(other.declaration["observations"][1])
    assert not list(other.cache.glob("observation-*.json"))


def test_actual_cli_child_failure_retains_diagnostics_and_no_success_receipt(fixture):
    from scripts.semantic_source_resolution import supervised

    directory = fixture["directory"]
    log = directory / "failed-phase.log"
    with pytest.raises(RepositoryError):
        supervised(
            [
                "summarize",
                "--inputs",
                str(directory / "nonexistent-audit"),
                "--output",
                str(directory / "not-published"),
                "--result",
                str(directory / "no-result.json"),
                "--log",
                str(log),
            ],
            log,
            seconds=20,
        )
    proof = assets.read_json(log.with_suffix(".log.json"), lambda: None)
    assert proof["completed"] is False and proof["capture"]["captured"] is True
    assert proof["capture"]["stderr_bytes"] > 0
    assert b'"error": "ASSET_JSON_LIMIT"' in log.read_bytes()
    assert proof["reason"] == "REPOSITORY_PARSE_FAILED"
    assert proof["observed_root"]["created"] is not None
    assert not (directory / "no-result.json").exists()
    assert not list(directory.glob(".supervision-*"))


def test_output_budget_counts_failed_attempts_and_preserves_prior_evidence(fixture, monkeypatch):
    root = fixture["directory"] / "outputs"
    monkeypatch.setattr(resolution, "OUTPUT_MAX_ENTRIES", 20)
    saved = {}
    for number in range(20):
        try:
            with resolution.output_store(root, "verify", lambda: None) as store:
                store.json("evidence.json", {"attempt": number})
                path = store.attempt / "evidence.json"
                saved[path] = path.read_bytes()
                raise RuntimeError("interrupted before publication")
        except RuntimeError:
            continue
        except SemanticError as exc:
            assert str(exc) == "SOURCE_OUTPUT_LIMIT"
            break
    else:
        pytest.fail("Repeated failed attempts must eventually exhaust the shared bound")
    assert len(saved) >= 2
    assert all(path.read_bytes() == raw for path, raw in saved.items())
    assert sum(1 for _ in root.rglob("*")) <= 20
    assert not list(root.glob("*/manifest.json"))


def test_output_budget_reserves_atomic_peak_before_writing(fixture, monkeypatch):
    root = fixture["directory"] / "byte-budget"
    monkeypatch.setattr(resolution, "OUTPUT_MAX_BYTES", 16 * 1024)
    with resolution.output_store(root, "verify", lambda: None) as store:
        store.json("small.json", {"value": 1})
        before = (store.attempt / "small.json").read_bytes()
        with pytest.raises(SemanticError, match="SOURCE_OUTPUT_LIMIT"):
            store.json("too-large.json", {"value": "x" * (9 * 1024)})
        assert (store.attempt / "small.json").read_bytes() == before
        assert not (store.attempt / "too-large.json").exists()
        assert not list(store.attempt.glob(".unit-part-*"))


def test_output_budget_includes_abandoned_temporary_bytes(fixture, monkeypatch):
    root = fixture["directory"] / "abandoned"
    partial = root / ".resolve-v2-abandoned"
    partial.mkdir(parents=True)
    (partial / ".unit-part-old").write_bytes(b"x" * 9000)
    monkeypatch.setattr(resolution, "OUTPUT_MAX_BYTES", 12 * 1024)
    with pytest.raises(SemanticError, match="SOURCE_OUTPUT_LIMIT"):
        with resolution.output_store(root, "verify", lambda: None):
            pytest.fail("Accumulated old bytes must be checked before a new attempt")
    assert (partial / ".unit-part-old").stat().st_size == 9000


def test_output_budget_rejects_hardlink_without_touching_its_target(fixture):
    root = fixture["directory"] / "linked-output"
    root.mkdir()
    target = fixture["directory"] / "original-evidence"
    target.write_bytes(b"retain")
    os.link(target, root / "linked")
    with pytest.raises(SemanticError, match="SOURCE_OUTPUT_SPECIAL_FILE"):
        with resolution.output_store(root, "verify", lambda: None):
            pytest.fail("Shared inode must not enter the writable result store")
    assert target.read_bytes() == b"retain"


def test_cli_refuses_log_inside_protected_input_before_launch(fixture):
    directory = fixture["directory"]
    protected = directory / "old-audit"
    protected.mkdir()
    log = protected / "new-log.txt"
    result = subprocess.run(
        [
            os.sys.executable,
            "-B",
            "-m",
            "scripts.semantic_source_resolution",
            "summarize",
            "--inputs",
            str(protected),
            "--log",
            str(log),
            "--result",
            str(directory / "result.json"),
            "--output",
            str(directory / "new-output"),
        ],
        cwd=assets.ROOT,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1 and b"SOURCE_OUTPUT_OVERLAP" in result.stderr
    assert not list(protected.iterdir())
    assert not (directory / "new-output").exists()


def test_phase_memory_refusal_retains_semantic_reason_and_owned_cleanup(fixture):
    from scripts.semantic_source_resolution import supervised

    log = fixture["directory"] / "memory-refusal.log"
    with pytest.raises(SemanticError, match="SOURCE_PHASE_MEMORY_LIMIT"):
        supervised(["summarize"], log, seconds=10, rss_limit=1)
    proof = assets.read_json(log.with_suffix(".log.json"), lambda: None)
    assert proof["completed"] is False and proof["reason"] == "SOURCE_PHASE_MEMORY_LIMIT"
    assert proof["capture"]["captured"]
    identity = proof["observed_root"]
    assert not resolution.native_runtime._live(identity["pid"], identity["created"])
    assert not list(fixture["directory"].glob(".supervision-*"))


def test_bounded_store_publishes_real_manifest_and_keeps_failed_attempt(fixture):
    root = fixture["directory"] / "publication"
    with pytest.raises(RuntimeError):
        with resolution.output_store(root, "verify", lambda: None) as failed:
            failed.json("evidence.json", {"complete": False})
            old_path = failed.attempt / "evidence.json"
            old_raw = old_path.read_bytes()
            raise RuntimeError("retain failed attempt")
    with resolution.output_store(root, "verify", lambda: None) as completed:
        completed.json("evidence.json", {"complete": True})
        target = completed.publish({"version": "fixture"}, ["evidence.json"])
    manifest = resolution.original.artifacts.verify(target)
    assert manifest["version"] == "fixture"
    assert assets.read_json(target / "evidence.json", lambda: None) == {"complete": True}
    assert old_path.read_bytes() == old_raw


def test_streamed_result_checks_existing_unflushed_bytes_before_next_write(fixture, monkeypatch):
    root = fixture["directory"] / "stream-limit"
    monkeypatch.setattr(resolution, "OUTPUT_MAX_BYTES", 16 * 1024)
    with resolution.output_store(root, "verify", lambda: None) as store:
        with store.lines("observations.jsonl") as stream:
            store.append(stream, b"a" * 9000)
            with pytest.raises(SemanticError, match="SOURCE_OUTPUT_LIMIT"):
                store.append(stream, b"b" * 9000)
        assert store.path("observations.jsonl").read_bytes() == b"a" * 9000


def measurement_junit(on_windows):
    from packages.semantic import source_benchmark as measurement

    root = ET.Element("testsuites")
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "tests": str(len(measurement.REQUIRED_CASES)),
            "failures": "0",
            "errors": "0",
            "skipped": "1" if on_windows else "0",
        },
    )
    for module, name in sorted(measurement.REQUIRED_CASES):
        case = ET.SubElement(suite, "testcase", {"classname": module, "name": name})
        if on_windows and name == measurement.POSIX_CASE:
            ET.SubElement(case, "skipped")
    return root, suite


@pytest.mark.parametrize("on_windows", [False, True])
def test_measurement_junit_requires_genuine_platform_coverage(on_windows):
    from packages.semantic import source_benchmark as measurement

    root, _ = measurement_junit(on_windows)
    result = measurement.junit_result(ET.tostring(root), on_windows=on_windows)
    assert result["passed"] == len(measurement.REQUIRED_CASES) - int(on_windows)
    assert len(result["skipped"]) == int(on_windows)


@pytest.mark.parametrize(
    "tampering",
    [
        "missing",
        "duplicate",
        "skip",
        "failure",
        "summary",
        "entity",
        "missing_budget_success",
        "missing_budget_fatal",
    ],
)
def test_measurement_junit_rejects_incomplete_or_inconsistent_success(tampering):
    from packages.semantic import source_benchmark as measurement

    root, suite = measurement_junit(True)
    if tampering == "missing":
        suite.remove(suite[0])
    elif tampering.startswith("missing_budget_"):
        identity = (
            "test_nested_target_preserves_parent_remaining_active_budget["
            + ("success" if tampering.endswith("success") else "fatal")
            + "]"
        )
        suite.remove(next(row for row in suite if row.attrib["name"] == identity))
        suite.set("tests", str(len(suite)))
    elif tampering == "duplicate":
        suite.append(copy.deepcopy(suite[0]))
    elif tampering == "skip":
        ET.SubElement(suite[0], "skipped")
    elif tampering == "failure":
        ET.SubElement(suite[0], "failure")
    elif tampering == "summary":
        suite.set("errors", "1")
    raw = ET.tostring(root)
    if tampering == "entity":
        raw = b'<!DOCTYPE x [<!ENTITY x "expanded">]>' + raw
    with pytest.raises(SemanticError):
        measurement.junit_result(raw, on_windows=True)


def measurement_runs(cached_seconds):
    from packages.semantic import source_benchmark as measurement

    return [
        {
            "index": index,
            "pair": pair,
            "cached": cached,
            "seconds": cached_seconds if cached else 10.0,
            "sampled_peak_rss_bytes": 1024**2,
            "cleanup_complete": True,
            "complete_original_observations_equal": True,
            "commands": {
                "command_attempts": 12,
                "cleanup_confirmed": 12,
                "ancestor_hits": int(cached),
            },
            "blob_bytes_end": 0,
            "blob_entries_end": 0,
            "ancestry_entries_end": 0,
        }
        for index, (pair, cached) in enumerate(measurement.run_order(), 1)
    ]


@pytest.mark.parametrize("seconds,enabled", [(8.0, True), (8.01, False), (12.0, False)])
def test_measurement_adoption_uses_complete_matched_batches_and_retains_negative(seconds, enabled):
    from packages.semantic import source_benchmark as measurement

    choice = measurement.decision(measurement_runs(seconds))
    assert choice["cache_enabled"] is enabled
    assert choice["reference_median_seconds"] == 10
    assert choice["cached_median_seconds"] == seconds


@pytest.mark.parametrize(
    "tampering", ["partial", "order", "nan", "cleanup", "equality", "commands", "cache_bound"]
)
def test_measurement_adoption_refuses_invalid_runs_even_when_reported_fast(tampering):
    from packages.semantic import source_benchmark as measurement

    runs = measurement_runs(1.0)
    if tampering == "partial":
        runs.pop()
    elif tampering == "order":
        runs.reverse()
    elif tampering == "nan":
        runs[0]["seconds"] = float("nan")
    elif tampering == "cleanup":
        runs[0]["cleanup_complete"] = False
    elif tampering == "equality":
        runs[0]["complete_original_observations_equal"] = False
    elif tampering == "commands":
        runs[0]["commands"]["cleanup_confirmed"] = 11
    else:
        runs[0]["blob_bytes_end"] = resolution.BLOB_CACHE_BYTES + 1
    with pytest.raises(SemanticError):
        measurement.decision(runs)


def sharing_error():
    error = OSError("owned result directory is shared")
    error.winerror = 32
    return error


def test_publication_recovers_only_bounded_sharing_failure(fixture, monkeypatch):
    with resolution.output_store(fixture["directory"] / "retry", "verify", lambda: None) as store:
        store.json("evidence.json", {"valid": True})
        original_rename = type(store.attempt).rename
        if os.name == "nt":
            ready = fixture["directory"] / "holder-ready"
            with subprocess.Popen(
                [
                    os.sys.executable,
                    "-c",
                    "import sys,time;from pathlib import Path;"
                    "Path(sys.argv[1]).write_text('ready');time.sleep(.4)",
                    str(ready),
                ],
                cwd=store.attempt,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as holder:
                stop = time.monotonic() + 3
                while not ready.exists():
                    assert time.monotonic() < stop and holder.poll() is None
                    time.sleep(0.01)
                target = store.publish({"version": "fixture"}, ["evidence.json"])
                holder.wait(timeout=3)
            assert store.publication_retries >= 1
        else:

            def rename(path, target):
                if path == store.attempt and store.publication_retries < 2:
                    raise sharing_error()
                return original_rename(path, target)

            monkeypatch.setattr(type(store.attempt), "rename", rename)
            target = store.publish({"version": "fixture"}, ["evidence.json"])
            assert store.publication_retries == 2
        assert resolution.original.artifacts.verify(target)["version"] == "fixture"


@pytest.mark.parametrize("failure", ["busy", "permission", "drift"])
def test_publication_refusal_preserves_sealed_attempt_and_never_publishes_bad_bytes(
    fixture, monkeypatch, failure
):
    root = fixture["directory"] / "refusal"
    monkeypatch.setattr(resolution, "PUBLICATION_DELAYS", (0, 0))
    with resolution.output_store(root, "verify", lambda: None) as store:
        store.json("evidence.json", {"valid": True})
        old = store.path("evidence.json").read_bytes()

        def rename(path, target):
            assert path == store.attempt
            if failure == "drift":
                store.path("evidence.json").write_bytes(b"modified")
            if failure == "permission":
                raise PermissionError("ordinary denial must not be retried")
            raise sharing_error()

        monkeypatch.setattr(type(store.attempt), "rename", rename)
        code = {
            "busy": "SOURCE_PUBLICATION_BUSY",
            "permission": "SOURCE_PUBLICATION_FAILED",
            "drift": "SOURCE_PUBLICATION_DRIFT",
        }[failure]
        with pytest.raises(SemanticError, match=code):
            store.publish({"version": "fixture"}, ["evidence.json"])
        assert (store.attempt / "manifest.json").is_file()
        assert not [path for path in root.iterdir() if not path.name.startswith(".")]
        if failure != "drift":
            assert store.path("evidence.json").read_bytes() == old
        assert store.publication_retries == (
            0 if failure == "permission" else 1 if failure == "drift" else 2
        )
