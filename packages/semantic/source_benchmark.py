"""Current-source runtime safety and fixed-eligibility cache measurement closure."""

import copy
import hashlib
import json
import math
import os
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import psutil

from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical
from packages.semantic import native_runtime
from packages.semantic import source_resolution as source

TEST_FILES = ("tests/test_semantic_native_runtime.py", "tests/test_semantic_source_resolution.py")
SAFETY_SOURCES = (*source.SOURCES, *TEST_FILES)
POSIX_CASE = "test_unreaped_posix_children_are_not_reregistered_as_live"
REQUIRED_CASES = {
    ("tests.test_semantic_native_runtime", name)
    for name in (
        "test_exact_bytes_and_allowed_exit",
        "test_real_inner_deadline_cleans_child_and_grandchild",
        "test_cancel_with_tree_utility_denied_still_cleans_owned_tree",
        "test_normal_root_exit_also_cleans_observed_descendants",
        "test_root_identity_drift_never_enumerates_replacement_children",
        "test_long_phase_retires_confirmed_exited_children_instead_of_hitting_lifetime_cap",
        "test_real_windows_process_signal_and_pid_binding",
        "test_new_parent_checkpoint_rejects_identity_before_liveness_query",
        "test_parent_observation_failure_is_fatal_semantic_error",
        POSIX_CASE,
    )
} | {
    ("tests.test_semantic_source_resolution", name)
    for name in (
        "test_context_parse_timeout_keeps_explicit_gap_and_fatal_cleanup_aborts",
        "test_output_budget_counts_failed_attempts_and_preserves_prior_evidence",
        "test_phase_memory_refusal_retains_semantic_reason_and_owned_cleanup",
        "test_nested_target_preserves_parent_remaining_active_budget[success]",
        "test_nested_target_preserves_parent_remaining_active_budget[fatal]",
        "test_real_target_timeout_cold_and_warm_preserve_same_two_file_trace",
    )
}
SAFETY_FILES = {"safety.json", "junit.xml", "tests.log"}


def platform():
    return {"sys_platform": sys.platform, "os_name": os.name}


def junit_result(raw, *, on_windows):
    source.original.require(
        0 < len(raw) <= source.original.MAX_UNIT
        and b"<!DOCTYPE" not in raw.upper()
        and b"<!ENTITY" not in raw.upper(),
        "SOURCE_SAFETY_JUNIT_INVALID",
    )
    try:
        root = ET.fromstring(raw)
        suites = root.findall("testsuite")
        source.original.require(
            root.tag == "testsuites" and len(suites) == 1, "SOURCE_SAFETY_JUNIT_INVALID"
        )
        cases = root.findall(".//testcase")
        identities = [(row.attrib["classname"], row.attrib["name"]) for row in cases]
        skipped = [
            (row.attrib["classname"], row.attrib["name"])
            for row in cases
            if row.find("skipped") is not None
        ]
        source.original.require(
            bool(cases)
            and len(identities) == len(set(identities))
            and REQUIRED_CASES
            <= set(identities) | {(module, name.split("[")[0]) for module, name in identities}
            and {module for module, _ in identities}
            == {"tests.test_semantic_native_runtime", "tests.test_semantic_source_resolution"}
            and not root.findall(".//failure")
            and not root.findall(".//error")
            and skipped
            == ([("tests.test_semantic_native_runtime", POSIX_CASE)] if on_windows else [])
            and int(suites[0].attrib["tests"]) == len(cases)
            and int(suites[0].attrib["failures"]) == 0
            and int(suites[0].attrib["errors"]) == 0
            and int(suites[0].attrib["skipped"]) == len(skipped),
            "SOURCE_SAFETY_TESTS_INCOMPLETE",
        )
    except (ET.ParseError, KeyError, ValueError, TypeError) as exc:
        if isinstance(exc, source.original.SemanticError):
            raise
        raise source.original.SemanticError("SOURCE_SAFETY_JUNIT_INVALID") from exc
    return {
        "collected": len(cases),
        "passed": len(cases) - len(skipped),
        "skipped": [list(identity) for identity in skipped],
        "identities": [list(identity) for identity in identities],
        "failures": 0,
        "errors": 0,
    }


def safety(output, check=native_runtime.checkpoint):
    before = source.original.artifacts.sources(SAFETY_SOURCES)
    executing = source.execution()
    with source.output_store(output, "safety", check) as store:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DG_", "PYTEST_", "GIT_")) and key != "SSH_AUTH_SOCK"
        }
        parent = psutil.Process()
        environment.update(
            DG_SEMANTIC_PARENT=str(parent.pid),
            DG_SEMANTIC_PARENT_TIME=str(parent.create_time()),
            PYTHONPATH=str(assets.ROOT),
            PYTHONDONTWRITEBYTECODE="1",
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        )

        def capture(stdout, stderr, truncated):
            with store.lines("tests.log") as stream:
                store.append(stream, stdout + (b"\n[bounded stderr]\n" + stderr if stderr else b""))
            source.original.require(not any(truncated), "SOURCE_SAFETY_LOG_LIMIT")

        def checked():
            check()
            store.ensure()

        native_runtime.run(
            [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                *[str(assets.ROOT / name) for name in TEST_FILES],
                "-q",
                "--tb=short",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                "--junitxml=" + str(store.path("junit.xml")),
            ],
            directory=store.attempt,
            environment=environment,
            check=checked,
            deadline=time.monotonic() + 600,
            limit=8 * 1024**2 - 65536 - 1024,
            capture=capture,
        )
        result = junit_result(store.path("junit.xml").read_bytes(), on_windows=os.name == "nt")
        source.original.require(
            source.original.artifacts.sources(SAFETY_SOURCES) == before
            and source.execution() == executing,
            "SOURCE_SAFETY_EXECUTION_DRIFT",
        )
        store.json(
            "safety.json",
            {
                "version": source.VERSION + "-runtime-safety",
                "execution": executing,
                "test_sources": before,
                "platform": platform(),
                "test_result": result,
            },
        )
        return store.publish(
            {"version": source.VERSION + "-runtime-safety", "sources": executing["sources"]},
            sorted(SAFETY_FILES),
        )


def _path(directory, prefix, name):
    source.original.require(
        isinstance(name, str)
        and Path(name).name == name
        and name not in {".", ".."}
        and "\\" not in name,
        "SOURCE_BENCHMARK_PATH",
    )
    return assets.safe_path(Path(directory) / (prefix + name))


def _verify_files(manifest, directory, prefix, check):
    assets.verify_seal(manifest)
    source.original.require(len(manifest["files"]) <= 64, "SOURCE_BENCHMARK_INVALID")
    for name, expected in manifest["files"].items():
        source.original.require(
            assets.file_digest(
                _path(directory, prefix, name), check, maximum=source.original.MAX_UNIT
            )
            == expected,
            "SOURCE_BENCHMARK_PAYLOAD_DRIFT",
        )


def verify_safety(directory, check=native_runtime.checkpoint, *, manifest=None, prefix=""):
    directory = assets.safe_path(Path(directory))
    manifest = source.original.artifacts.verify(directory, check) if manifest is None else manifest
    _verify_files(manifest, directory, prefix, check)
    source.original.require(
        manifest["version"] == source.VERSION + "-runtime-safety"
        and set(manifest["files"]) == SAFETY_FILES,
        "SOURCE_SAFETY_INVALID",
    )
    value = assets.read_json(
        _path(directory, prefix, "safety.json"), check, maximum=source.original.MAX_UNIT
    )
    source.original.require(
        value["version"] == manifest["version"]
        and value["execution"] == source.execution()
        and value["execution"]["sources"] == manifest["sources"]
        and value["test_sources"] == source.original.artifacts.sources(SAFETY_SOURCES)
        and value["platform"] == platform(),
        "SOURCE_SAFETY_EXECUTION_DRIFT",
    )
    actual = junit_result(
        _path(directory, prefix, "junit.xml").read_bytes(), on_windows=os.name == "nt"
    )
    source.original.require(value["test_result"] == actual, "SOURCE_SAFETY_RESULT_DRIFT")
    return manifest, value


def run_order():
    return [
        (pair + 1, cached)
        for pair in range(3)
        for cached in ([False, True] if pair % 2 == 0 else [True, False])
    ]


def decision(runs):
    source.original.require(len(runs) == 6, "SOURCE_BENCHMARK_INCOMPLETE")
    for index, (run, (pair, cached)) in enumerate(zip(runs, run_order(), strict=True), 1):
        source.original.require(
            type(run["index"]) is int
            and run["index"] == index
            and type(run["pair"]) is int
            and run["pair"] == pair
            and run["cached"] is cached,
            "SOURCE_BENCHMARK_ORDER_DRIFT",
        )
        source.original.require(
            type(run["seconds"]) in (int, float)
            and math.isfinite(run["seconds"])
            and run["seconds"] > 0
            and type(run["sampled_peak_rss_bytes"]) is int
            and 0 < run["sampled_peak_rss_bytes"] <= 6 * 1024**3
            and run["cleanup_complete"] is True
            and run["complete_original_observations_equal"] is True
            and type(run["commands"].get("command_attempts")) is int
            and run["commands"]["command_attempts"] > 0
            and run["commands"]["command_attempts"] == run["commands"].get("cleanup_confirmed")
            and all(type(value) is int and value >= 0 for value in run["commands"].values())
            and type(run["blob_bytes_end"]) is int
            and 0 <= run["blob_bytes_end"] <= source.BLOB_CACHE_BYTES
            and type(run["blob_entries_end"]) is int
            and 0 <= run["blob_entries_end"] <= source.BLOB_CACHE_ENTRIES
            and type(run["ancestry_entries_end"]) is int
            and 0 <= run["ancestry_entries_end"] <= source.ANCESTRY_CACHE_ENTRIES,
            "SOURCE_BENCHMARK_RESOURCE_OR_EQUALITY",
        )
        hits = run["commands"].get("blob_hits", 0) + run["commands"].get("ancestor_hits", 0)
        source.original.require(
            hits > 0
            if cached
            else hits == 0
            and run["blob_entries_end"] == 0
            and run["blob_bytes_end"] == 0
            and run["ancestry_entries_end"] == 0,
            "SOURCE_BENCHMARK_CACHE_MODE_DRIFT",
        )
    reference = [run["seconds"] for run in runs if not run["cached"]]
    cached = [run["seconds"] for run in runs if run["cached"]]
    median_reference, median_cached = statistics.median(reference), statistics.median(cached)
    improvement = (median_reference - median_cached) / median_reference
    return {
        "reference_median_seconds": median_reference,
        "cached_median_seconds": median_cached,
        "reference_range_seconds": [min(reference), max(reference)],
        "cached_range_seconds": [min(cached), max(cached)],
        "median_improvement": improvement,
        "minimum_improvement": 0.2,
        "cache_enabled": improvement >= 0.2,
    }


class FrozenContext(source.original.Context):
    """Original algorithm, with original immutable target eligibility on both arms."""

    def __init__(self, declaration, reader, expected, check):
        self.declaration, self.reader, self.expected, self.check = (
            declaration,
            reader,
            expected,
            check,
        )
        self.targets = {row["commit_id"]: row for row in declaration["targets"]}
        self.units, self.metadata, self.used_cached = {}, {}, set()
        self.reused = 0

    def target(self, sha):
        self.check()
        source.original.require(sha in self.expected["target_evidence"], "SOURCE_BENCHMARK_TARGET")
        return self.expected["target_evidence"][sha]

    def save(self, kind, sha, value):
        source.original.require(
            kind == "observation" and sha == self.expected["metadata"]["commit_id"]
        )
        return copy.deepcopy(value)


def _rss():
    current = psutil.Process()
    total = current.memory_info().rss
    try:
        for child in current.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.NoSuchProcess:
                continue
    except psutil.Error as exc:
        raise source.original.SemanticError("SOURCE_BENCHMARK_OBSERVATION_FAILED") from exc
    return total


def _unit_name(index, unit):
    return f"run-{index:02}-unit-{unit:02}.json"


def benchmark(
    declaration, acquisition, audit, safety_input, output, check=native_runtime.checkpoint
):
    declaration, acquisition, audit, safety_input, output = source._separate(
        declaration, acquisition, audit, safety_input, output
    )
    bound = source.load_declaration(declaration, check)
    old, plan = bound["original"], bound["input_plan"]
    safety_manifest, _ = verify_safety(safety_input, check)
    source.original.require(
        source.original.artifacts.verify(audit, check) == bound["original_manifest"]
    )
    previous = source._rows(audit / "observations.jsonl", check)
    source._plan_matches(plan, old, previous)
    by_id = {row["metadata"]["commit_id"]: row for row in previous}
    source.original.require(
        source.original._inventory(acquisition, check) == old["source_inventory"], "SOURCE_DRIFT"
    )
    runs = []
    with source.output_store(output, "benchmark", check) as store:
        for name in sorted(SAFETY_FILES):
            store.copy(name, safety_input / name)
        store.json("resolution.json", bound)
        store.copy("original-observations.jsonl", audit / "observations.jsonl")
        names = [*sorted(SAFETY_FILES), "resolution.json", "original-observations.jsonl"]
        for index, (pair, cached) in enumerate(run_order(), 1):
            start_record = {"index": index, "pair": pair, "cached": cached}
            start_name = f"run-{index:02}-start.json"
            store.json(start_name, start_record)
            names.append(start_name)
            peak, last_sample = 0, -math.inf

            def checked():
                nonlocal peak, last_sample
                check()
                if time.monotonic() - last_sample >= 0.1:
                    peak = max(peak, _rss())
                    last_sample = time.monotonic()
                    source.original.require(peak <= 6 * 1024**3, "SOURCE_BENCHMARK_MEMORY")

            work = store.attempt / f"native-{index:02}"
            store.ensure(entries=1)
            work.mkdir()
            reader = source.Reader(
                acquisition / "source.git",
                work,
                source.original.native_environment(work, acquisition / "source.git"),
                checked,
                time.monotonic() + 30,
                source_id=bound["content_hash"],
                cache=cached,
            )
            started = time.monotonic()
            hashes = []
            for unit, sha in enumerate(plan["benchmark_completed_observation_ids"], 1):
                expected = by_id[sha]
                digest = hashlib.sha256(canonical(expected)).hexdigest()
                actual = FrozenContext(old, reader, expected, checked).observation(
                    expected["metadata"]
                )
                name = _unit_name(index, unit)
                store.json(name, actual)
                names.append(name)
                source.original.require(
                    actual == expected
                    and hashlib.sha256(canonical(expected)).hexdigest() == digest,
                    "SOURCE_BENCHMARK_LINE_DRIFT",
                )
                hashes.append(digest)
            elapsed = time.monotonic() - started
            checked()
            work.rmdir()
            row = start_record | {
                "seconds": elapsed,
                "sampled_peak_rss_bytes": peak,
                "cleanup_complete": reader.statistics["command_attempts"]
                == reader.statistics["cleanup_confirmed"],
                "complete_original_observations_equal": True,
                "commands": dict(reader.statistics),
                "observation_hashes": hashes,
                "blob_entries_end": len(reader.blobs),
                "blob_bytes_end": reader.blob_bytes,
                "ancestry_entries_end": len(reader.ancestry),
            }
            name = f"run-{index:02}.json"
            store.json(name, row)
            names.append(name)
            runs.append(row)
            print(
                json.dumps({"benchmark_run": index, "cached": cached, "seconds": elapsed}),
                flush=True,
            )
        choice = decision(runs)
        source.original.require(source.execution() == bound["execution"], "SOURCE_EXECUTION_DRIFT")
        source.original.require(
            source.original._inventory(acquisition, check) == old["source_inventory"],
            "SOURCE_DRIFT",
        )
        source.original.require(
            verify_safety(safety_input, check)[0] == safety_manifest
            and source.load_declaration(declaration, check) == bound
            and source.original.artifacts.verify(audit, check) == bound["original_manifest"],
            "SOURCE_BENCHMARK_INPUT_DRIFT",
        )
        store.json(
            "benchmark.json",
            {
                "version": source.VERSION + "-cache-benchmark",
                "declaration_hash": bound["content_hash"],
                "safety_manifest": safety_manifest,
                "platform": platform(),
                "decision": choice,
                "original_target_eligibility_frozen": True,
                "qualification_promoted": False,
            },
        )
        names.append("benchmark.json")
        return store.publish(
            {
                "version": source.VERSION + "-cache-benchmark",
                "sources": bound["execution"]["sources"],
                "assembled_at": datetime.now(UTC).isoformat(),
            },
            names,
        )


def verify_benchmark(directory, check=native_runtime.checkpoint, *, manifest=None, prefix=""):
    directory = assets.safe_path(Path(directory))
    manifest = source.original.artifacts.verify(directory, check) if manifest is None else manifest
    _verify_files(manifest, directory, prefix, check)
    names = {*SAFETY_FILES, "resolution.json", "original-observations.jsonl", "benchmark.json"}
    for index in range(1, 7):
        names.update({f"run-{index:02}-start.json", f"run-{index:02}.json"})
        names.update(_unit_name(index, unit) for unit in (1, 2))
    source.original.require(
        manifest["version"] == source.VERSION + "-cache-benchmark"
        and set(manifest["files"]) == names,
        "SOURCE_BENCHMARK_INVALID",
    )

    def read(name):
        return assets.read_json(
            _path(directory, prefix, name), check, maximum=source.original.MAX_UNIT
        )

    bound = read("resolution.json")
    source._verify_declaration(bound)
    value = read("benchmark.json")
    source.original.require(
        value["version"] == manifest["version"]
        and value["declaration_hash"] == bound["content_hash"]
        and manifest["sources"] == bound["execution"]["sources"]
        and value["original_target_eligibility_frozen"] is True
        and value["qualification_promoted"] is False
        and value["platform"] == platform()
        and manifest["files"]["original-observations.jsonl"]
        == bound["original_manifest"]["files"]["observations.jsonl"],
        "SOURCE_BENCHMARK_BINDING_DRIFT",
    )
    verify_safety(directory, check, manifest=value["safety_manifest"], prefix=prefix)
    original_rows = source._rows(_path(directory, prefix, "original-observations.jsonl"), check)
    source._plan_matches(bound["input_plan"], bound["original"], original_rows)
    by_id = {row["metadata"]["commit_id"]: row for row in original_rows}
    runs = []
    for index, (pair, cached) in enumerate(run_order(), 1):
        source.original.require(
            read(f"run-{index:02}-start.json") == {"index": index, "pair": pair, "cached": cached},
            "SOURCE_BENCHMARK_ORDER_DRIFT",
        )
        row = read(f"run-{index:02}.json")
        hashes = []
        for unit, sha in enumerate(bound["input_plan"]["benchmark_completed_observation_ids"], 1):
            actual = read(_unit_name(index, unit))
            source.original.require(actual == by_id[sha], "SOURCE_BENCHMARK_LINE_DRIFT")
            hashes.append(hashlib.sha256(canonical(actual)).hexdigest())
        source.original.require(row["observation_hashes"] == hashes, "SOURCE_BENCHMARK_LINE_DRIFT")
        runs.append(row)
    source.original.require(value["decision"] == decision(runs), "SOURCE_BENCHMARK_DECISION_DRIFT")
    return manifest, value
