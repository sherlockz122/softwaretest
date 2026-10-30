"""Static approved v1/v3 data dialects; no producer, parser, Git or model imports.

These reviewed source-level snapshots keep historical semantics independent from
producer runtime dependencies. Never load code from an evidence artifact.
"""

import copy
import hashlib
import math
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime

from packages.mining import fix_rules
from packages.semantic.policy import SemanticError

DIALECT_VERSION = "sealed-native-data-dialects-v1"
V1_VERSION = "native-message-szz-horizon-source-v1"
VERSION = "native-parent-consensus-source-v3"
START = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp())
TARGET_STOP = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
STOP = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp())
HORIZON, LAG = 90 * 86400, 7 * 86400
ROLES = ("train_candidate", "calibration_candidate", "threshold_candidate", "evaluation_candidate")
MAX_TARGETS, MAX_OBSERVATIONS, MAX_UNIT = 1200, 2048, 16 * 1024**2


def sha_valid(value):
    if not isinstance(value, bytes) or re.fullmatch(b"[0-9a-f]{40}", value) is None:
        raise SemanticError("ADMISSION_SHA_INVALID")
    return value.decode("ascii")


def object_hash(kind, body):
    return hashlib.sha1(f"{kind} {len(body)}\0".encode() + body).hexdigest()


def commit_clocks(raw, sha):
    if object_hash("commit", raw) != sha:
        raise SemanticError("ADMISSION_COMMIT_DRIFT")
    headers = raw.split(b"\n\n", 1)[0].splitlines()
    values = {
        kind: [line[len(kind) + 1 :] for line in headers if line.startswith(kind + b" ")]
        for kind in [b"author", b"committer", b"parent"]
    }
    if len(values[b"author"]) != 1 or len(values[b"committer"]) != 1:
        raise SemanticError("ADMISSION_CLOCK_INVALID")
    return {
        "commit_id": sha,
        "author_epoch": int(values[b"author"][0].rsplit(b" ", 2)[1]),
        "committer_epoch": int(values[b"committer"][0].rsplit(b" ", 2)[1]),
        "parents": [sha_valid(v) for v in values[b"parent"]],
    }


def require(condition, code="EVAL_SOURCE_INVALID"):
    if not condition:
        raise SemanticError(code)


def role(timestamp):
    year = datetime.fromtimestamp(timestamp, UTC).year
    return (
        ROLES[0] if 2020 <= year <= 2022 else ROLES[year - 2022] if 2023 <= year <= 2025 else None
    )


def verify_observation(value, declaration):
    """Check exported decisions against preserved line facts; Git replay stays separate."""
    metadata, trace = value["metadata"], value["trace"]
    targets = {r["commit_id"]: r for r in declaration["targets"]}
    positive, gaps, foreign = set(), set(), set()
    expected_gaps = (
        trace["reasons"] or ["unmapped_observation"]
        if any(f["status"] == "unknown" for f in trace["files"])
        or trace["status"] == "unknown"
        and not trace["files"]
        else []
    )
    for link in trace["links"]:
        identity = link["blamed_sha"]
        require(
            link["fix_sha"] == metadata["commit_id"]
            and metadata["parents"] == [link["parent_sha"]]
            and isinstance(link["eligible"], bool)
            and re.fullmatch("[0-9a-f]{64}", link["line_hash"])
            and type(link["fixed_line"]) is int
            and link["fixed_line"] > 0
            and type(link["blamed_line"]) is int
            and link["blamed_line"] > 0
            and link["label_available_at"]
            == datetime.fromtimestamp(metadata["committer_epoch"] + LAG, UTC).isoformat(),
            "EVAL_LINE_EVIDENCE_DRIFT",
        )
        if identity in targets:
            evidence = value["target_evidence"][identity]
            require(
                evidence["metadata"] | {"role": targets[identity]["role"]} == targets[identity],
                "EVAL_TARGET_EVIDENCE_DRIFT",
            )
            if link["eligible"]:
                require(
                    evidence["parse_status"] == "parsed"
                    and link["reason"] is None
                    and targets[identity]["committer_epoch"] <= metadata["committer_epoch"],
                    "EVAL_TARGET_EVIDENCE_DRIFT",
                )
                positive.add(identity)
            else:
                require(link["reason"] is not None, "EVAL_LINE_EVIDENCE_DRIFT")
                gaps.add(identity)
        else:
            require(
                identity in declaration["reachable_metadata"] and link["eligible"] is False,
                "EVAL_FOREIGN_BLAME",
            )
            foreign.add(identity)
    outside = set(value["outside_frame"])
    require(outside <= foreign, "EVAL_LINE_EVIDENCE_DRIFT")
    if foreign - outside:
        expected_gaps = [*expected_gaps, "outside_identity_unverified"]
    require(
        value["positive_targets"] == sorted(positive)
        and value["target_gaps"] == sorted(gaps)
        and value["global_gaps"] == sorted(set(expected_gaps))
        and set(value["target_evidence"]) == positive | gaps,
        "EVAL_LINE_EVIDENCE_DRIFT",
    )


def outcomes(declaration, observations):
    require(len(observations) == len(declaration["observations"]), "EVAL_OBSERVATIONS_INCOMPLETE")
    expected = {r["commit_id"]: r for r in declaration["observations"]}
    actual = {r["metadata"]["commit_id"]: r for r in observations}
    require(
        len(actual) == len(observations) and set(actual) == set(expected),
        "EVAL_OBSERVATIONS_INCOMPLETE",
    )
    require(all(actual[k]["metadata"] == v for k, v in expected.items()), "EVAL_OBSERVATION_DRIFT")
    targets = {r["commit_id"] for r in declaration["targets"]}
    require(
        all(
            set(r["positive_targets"]) <= targets and set(r["target_gaps"]) <= targets
            for r in observations
        ),
        "EVAL_FOREIGN_TARGET",
    )
    result = []
    for target in declaration["targets"]:
        stop = target["committer_epoch"] + HORIZON
        require(stop < STOP, "EVAL_HORIZON_UNCOVERED")
        within = [
            r
            for r in observations
            if target["committer_epoch"] <= r["metadata"]["committer_epoch"] <= stop
        ]
        positives = sorted(
            (r["metadata"] for r in within if target["commit_id"] in r["positive_targets"]),
            key=lambda r: (r["committer_epoch"], r["commit_id"]),
        )
        gaps = [
            r["metadata"]["commit_id"]
            for r in within
            if r["global_gaps"] or target["commit_id"] in r["target_gaps"]
        ]
        label = 1 if positives else None if gaps else 0
        known = (
            positives[0]["committer_epoch"] + LAG
            if positives
            else stop + LAG
            if label == 0
            else None
        )
        result.append(
            target
            | {
                "provisional_label": label,
                "positive_fix_ids": [r["commit_id"] for r in positives],
                "gap_fix_ids": gaps,
                "reconstructed_available_epoch": known,
                "actual_label_available_at": None,
                "gold": False,
            }
        )
    return result


class V1:
    VERSION = V1_VERSION
    START, TARGET_STOP, STOP = START, TARGET_STOP, STOP
    HORIZON, LAG, ROLES = HORIZON, LAG, ROLES
    MAX_TARGETS, MAX_OBSERVATIONS, MAX_UNIT = MAX_TARGETS, MAX_OBSERVATIONS, MAX_UNIT
    SemanticError = SemanticError
    require = staticmethod(require)
    commit_clocks = staticmethod(commit_clocks)
    role = staticmethod(role)
    verify_observation = staticmethod(verify_observation)
    outcomes = staticmethod(outcomes)


original = V1


def combine_edges(metadata, edges, declaration):
    """Derive a new observation from complete explicit one-parent edge views."""
    original.require(isinstance(metadata, dict) and isinstance(edges, list), "SOURCE_EDGE_INVALID")
    expected = {value["commit_id"]: value for value in declaration["observations"]}.get(
        metadata["commit_id"]
    )
    original.require(metadata == expected, "SOURCE_OBSERVATION_DRIFT")
    parents = metadata["parents"]
    original.require(len(parents) == len(set(parents)), "SOURCE_PARENT_EDGE_DRIFT")
    if not parents or len(parents) > 8:
        original.require(not edges, "SOURCE_PARENT_EDGE_DRIFT")
        return {
            "version": VERSION,
            "metadata": copy.deepcopy(metadata),
            "edges": [],
            "positive_targets": [],
            "target_gaps": [],
            "global_gaps": ["root_parent" if not parents else "parent_limit"],
            "outside_frame": [],
            "target_evidence": {},
        }
    original.require([edge["parent_sha"] for edge in edges] == parents, "SOURCE_PARENT_EDGE_DRIFT")
    positives = []
    concrete, ineligible, global_gaps, outside, target_evidence = set(), set(), set(), set(), {}
    for parent, edge in zip(parents, edges, strict=True):
        value = edge["observation"]
        original.require(
            value["metadata"] == metadata | {"parents": [parent]},
            "SOURCE_PARENT_EDGE_DRIFT",
        )
        # This validates an explicit edge view, not a fabricated raw commit object.
        original.verify_observation(value, declaration)
        positive, gaps = set(value["positive_targets"]), set(value["target_gaps"])
        positives.append(positive)
        concrete.update(positive | gaps)
        ineligible.update(gaps)
        global_gaps.update(value["global_gaps"])
        outside.update(value["outside_frame"])
        for identity, evidence in value["target_evidence"].items():
            original.require(
                identity not in target_evidence or target_evidence[identity] == evidence,
                "SOURCE_TARGET_EVIDENCE_DRIFT",
            )
            target_evidence[identity] = evidence
    consensus = set.intersection(*positives)
    return copy.deepcopy(
        {
            "version": VERSION,
            "metadata": metadata,
            "edges": edges,
            "positive_targets": sorted(consensus),
            "target_gaps": sorted((concrete - consensus) | ineligible),
            "global_gaps": sorted(global_gaps),
            "outside_frame": sorted(outside),
            "target_evidence": target_evidence,
        }
    )


def verify_resolution(value, declaration):
    expected = combine_edges(value["metadata"], value["edges"], declaration)
    original.require(value == expected, "SOURCE_RESOLUTION_DRIFT")


def decode_commit(raw, sha):
    original.require(object_hash("commit", raw) == sha, "SOURCE_OBJECT_DRIFT")
    metadata = original.commit_clocks(raw, sha)
    headers, separator, message = raw.partition(b"\n\n")
    original.require(bool(separator), "SOURCE_COMMIT_INVALID")
    try:
        encoding = next(
            (
                line[9:].decode("ascii")
                for line in headers.splitlines()
                if line.startswith(b"encoding ")
            ),
            "utf-8",
        )
        text = message.decode(encoding, "strict")
        if "\0" in text:
            text = None
    except (UnicodeError, LookupError):
        text = None
    return metadata | {"message_hash": hashlib.sha256(message).hexdigest()}, text


def _assessment(message, edges):
    # An unread edge must never turn a partly observed file union into docs-only.
    complete = all(edge["read_failure"] is None for edge in edges)
    files = [file for edge in edges for file in edge["files"]] if complete else []
    return fix_rules.evaluate(
        message or "", "parsed" if message is not None else "message_encoding", files, []
    )


def verify_target(value, declaration):
    targets = {row["commit_id"]: row for row in declaration["targets"]}
    metadata = value["metadata"]
    expected = targets.get(metadata["commit_id"])
    original.require(
        expected is not None
        and metadata | {"role": expected["role"]} == expected
        and value["parse_status"] in {"parsed", "unavailable", "merge_skipped", "message_encoding"}
        and isinstance(value["files"], list),
        "SOURCE_TARGET_EVIDENCE_DRIFT",
    )
    if value["parse_status"] == "unavailable":
        original.require(
            isinstance(value.get("reason"), str)
            and not value["reason"].startswith("NATIVE_")
            and not value["files"],
            "SOURCE_TARGET_EVIDENCE_DRIFT",
        )


def verify_unit(value, declaration):
    """Verify retained native identities and deterministic projection; replay is separate."""
    original.require(value["version"] == VERSION, "SOURCE_UNIT_VERSION")
    result = value["resolution"]
    metadata = result["metadata"]
    try:
        raw = bytes.fromhex(value["raw_commit_hex"])
    except (ValueError, TypeError) as exc:
        raise original.SemanticError("SOURCE_COMMIT_INVALID") from exc
    actual, message = decode_commit(raw, metadata["commit_id"])
    original.require(actual == metadata, "SOURCE_OBSERVATION_DRIFT")
    verify_resolution(result, declaration)
    edges = result["edges"]
    original.require(value["assessment"] == _assessment(message, edges), "SOURCE_ASSESSMENT_DRIFT")
    for edge in edges:
        original.require(
            isinstance(edge["files"], list)
            and len(edge["files"]) <= 4096
            and (edge["read_failure"] is None or isinstance(edge["read_failure"], str)),
            "SOURCE_NATIVE_FILE_DRIFT",
        )
        files = {file["ordinal"]: file for file in edge["files"]}
        original.require(len(files) == len(edge["files"]), "SOURCE_NATIVE_FILE_DRIFT")
        for file in edge["files"]:
            original.require(
                file["diff_hash"]
                == hashlib.sha256((file.get("diff_text") or "").encode()).hexdigest(),
                "SOURCE_NATIVE_FILE_DRIFT",
            )
        observation = edge["observation"]
        trace = observation["trace"]
        failure = edge["read_failure"] or ("message_encoding" if message is None else None)
        if failure:
            original.require(
                not failure.startswith("NATIVE_")
                and not edge["files"]
                and trace == {"status": "unknown", "reasons": [failure], "files": [], "links": []},
                "SOURCE_UNSAFE_FAILURE",
            )
        elif not value["assessment"]["rule_candidate"]:
            original.require(
                trace
                == {
                    "status": "not_candidate",
                    "reasons": ["frozen_non_candidate"],
                    "files": [],
                    "links": [],
                },
                "SOURCE_ASSESSMENT_DRIFT",
            )
        else:
            # A trace-wide timeout has no file facts; its explicit reason is retained.
            original.require(
                not trace["files"]
                and trace["status"] == "unknown"
                or [row["ordinal"] for row in trace["files"]] == list(files),
                "SOURCE_NATIVE_FILE_DRIFT",
            )
            for link in trace["links"]:
                candidates = [
                    file
                    for file in files.values()
                    if file["old_path"] == link["path"] and file["old_blob"] == link["old_blob"]
                ]
                original.require(
                    len(candidates) == 1
                    and link["fixed_line"]
                    in (candidates[0]["line_numbers"] or {}).get("deleted", []),
                    "SOURCE_NATIVE_FILE_DRIFT",
                )
    for target in result["target_evidence"].values():
        verify_target(target, declaration)


def _summary(declaration, observations, projected, pilot, *, cache_enabled=False):
    support = {
        role: dict(
            Counter(
                "unknown" if row["provisional_label"] is None else str(row["provisional_label"])
                for row in projected
                if row["role"] == role
            )
        )
        for role in original.ROLES
    }
    return {
        "version": VERSION,
        "complete_population": not pilot,
        "planned_gap_observations": len(declaration["input_plan"]["all_gap_observations"]),
        "attempted_gap_observations": sum(
            row["kind"] == "resolved"
            and row["value"]["resolution"]["metadata"]["commit_id"]
            in declaration["input_plan"]["all_gap_observations"]
            for row in observations
        ),
        "completed_observations": len(observations),
        "targets": len(declaration["original"]["targets"]),
        "support": support,
        "calibration_positive_min10": support[original.ROLES[1]].get("1", 0) >= 10,
        "threshold_positive_min10": support[original.ROLES[2]].get("1", 0) >= 10,
        "global_gap_observations": sum(
            bool(_projection(row)["global_gaps"]) for row in observations
        ),
        "target_gap_observations": sum(
            bool(_projection(row)["target_gaps"]) for row in observations
        ),
        "human_review_complete": False,
        "native14_features_verified": False,
        "new_representation_verified": False,
        "contamination_certified": False,
        "research_training_ready": False,
        "historical_replay": False,
        "candidate_release": False,
        "cache_enabled": cache_enabled,
    }


def _projection(row):
    return row["value"]["resolution"] if row["kind"] == "resolved" else row["value"]


def _plan_matches(plan, declaration, rows):
    by_id = {row["metadata"]["commit_id"]: row for row in rows}
    original.require(len(by_id) == len(rows), "SOURCE_OBSERVATION_DRIFT")
    gaps = sorted(sha for sha, row in by_id.items() if row["global_gaps"] or row["target_gaps"])
    unavailable = sorted(
        {
            sha
            for row in rows
            for sha, evidence in row["target_evidence"].items()
            if evidence["parse_status"] == "unavailable"
        }
    )
    original.require(
        plan["original_declaration_hash"] == declaration["content_hash"]
        and plan["all_gap_observations"] == gaps
        and plan["unavailable_target_ids"] == unavailable
        and len(gaps) == 84
        and len(declaration["targets"]) == 1120
        and len(rows) == 1971
        and plan["source_roles_or_members_changed"] is False
        and plan["label_or_model_scores_used_to_select_cases"] is False
        and plan["complete_gap_attempts_not_guaranteed_resolution"] is True,
        "SOURCE_PLAN_DRIFT",
    )
    pilot, benchmark = plan["pilot_observation_ids"], plan["benchmark_completed_observation_ids"]
    original.require(
        isinstance(pilot, list)
        and 0 < len(pilot) <= 16
        and len(set(pilot)) == len(pilot)
        and set(pilot) <= set(by_id)
        and isinstance(benchmark, list)
        and len(benchmark) == len(set(benchmark)) == 2
        and set(benchmark) <= set(pilot)
        and plan["benchmark_interleaved_pairs"] == 3
        and plan["cache_adoption_median_improvement_minimum"] == 0.2,
        "SOURCE_PLAN_DRIFT",
    )
    original.require(
        len(by_id[benchmark[0]]["trace"]["links"])
        == plan["benchmark_largest_original_line_links"]
        == max(len(row["trace"]["links"]) for row in rows),
        "SOURCE_BENCHMARK_DRIFT",
    )


class V3:
    VERSION = VERSION
    original = V1
    BLOB_CACHE_BYTES, BLOB_CACHE_ENTRIES, ANCESTRY_CACHE_ENTRIES = 8 * 1024**2, 512, 4096
    combine_edges = staticmethod(combine_edges)
    verify_resolution = staticmethod(verify_resolution)
    verify_unit = staticmethod(verify_unit)
    _summary = staticmethod(_summary)
    _projection = staticmethod(_projection)
    _plan_matches = staticmethod(_plan_matches)


source = V3


TEST_FILES = ("tests/test_semantic_native_runtime.py", "tests/test_semantic_source_resolution.py")

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


def _unit_name(index, unit):
    return f"run-{index:02}-unit-{unit:02}.json"


class Measurement:
    TEST_FILES, POSIX_CASE, REQUIRED_CASES, SAFETY_FILES = (
        TEST_FILES,
        POSIX_CASE,
        REQUIRED_CASES,
        SAFETY_FILES,
    )
    junit_result = staticmethod(junit_result)
    run_order = staticmethod(run_order)
    decision = staticmethod(decision)
    _unit_name = staticmethod(_unit_name)
