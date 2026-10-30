"""Controlled synthetic approvals exercise data semantics, never qualify a source."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import fix_rules
from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical, content_hash
from packages.semantic import evaluation_population as producer
from packages.semantic import source_evidence_reader as consumer
from packages.semantic.policy import SemanticError
from packages.semantic.source_evidence_contracts import V1, V3, Measurement
from tests import test_semantic_source_resolution as parent_cases
from tests.test_semantic_evaluation_population import observe, source_fixture


@pytest.fixture(scope="module")
def original_audit():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="sealed-source-tests-") as name:
        root = Path(name)
        with pytest.MonkeyPatch.context() as setup:
            fixture = source_fixture(root, setup)
            audit = observe(fixture, root)
            yield audit, root


@pytest.fixture
def approved_original(original_audit, monkeypatch):
    audit, root = original_audit
    manifest = json.loads((audit / "manifest.json").read_bytes())
    declaration = json.loads((audit / "declaration.json").read_bytes())
    catalog = copy.deepcopy(consumer._APPROVALS)
    approval = catalog["stage32-native-v1"]
    approval.update(
        manifest_hash=manifest["content_hash"],
        directory_id=audit.name,
        producer_version=manifest["version"],
        producer_sources_hash=content_hash(manifest["sources"]),
        producer_execution_hash=content_hash(declaration["execution"]),
    )
    # Only the trusted fixture catalog is substituted; data/execution checks stay real.
    monkeypatch.setattr(consumer, "_APPROVALS", catalog)
    return audit, root, manifest, declaration


def copied(audit, root, name):
    target = root / name / audit.name
    target.parent.mkdir()
    shutil.copytree(audit, target)
    return target


def test_catalog_is_read_only_technical_scope_and_has_no_host_paths():
    records = consumer.approved_records()
    assert {r["approval_id"] for r in records} == {"stage32-native-v1", "stage33-source6-v3"}
    encoded = json.dumps(records)
    assert "D:" not in encoded and "C:" not in encoded
    assert all(r["scope"] == "technical sealed historical source reading only" for r in records)
    assert all(not r["model_qualification"] and not r["human_review_complete"] for r in records)


def test_unapproved_input_is_refused_before_any_payload_open(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unknown approval must not read data")

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(consumer.ConsumptionError, match="SOURCE_CONSUME_NOT_APPROVED"):
        consumer.verify_history(assets.ROOT / "runtime/does-not-exist", "caller-self-approved")


@pytest.mark.parametrize(
    "approval,origin,code",
    [
        ("stage33-source6-v3", None, "SOURCE_CONSUME_ORIGIN_REQUIRED"),
        ("stage32-native-v1", "unexpected", "SOURCE_CONSUME_ORIGIN_UNEXPECTED"),
    ],
)
def test_origin_request_shape_is_checked_before_file_access(monkeypatch, approval, origin, code):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid origin request must not read data")

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(consumer.ConsumptionError, match=code):
        consumer.verify_history(assets.ROOT / "runtime/does-not-exist", approval, origin=origin)


def test_real_git_original_projection_reads_without_current_execution(
    approved_original, monkeypatch
):
    audit, _, _, _ = approved_original

    def forbidden(*args, **kwargs):
        raise AssertionError("Historical data cannot query current Git/execution")

    monkeypatch.setattr(producer, "_execution", forbidden)
    result = consumer.verify_history(audit, "stage32-native-v1")
    stored = json.loads((audit / "outcomes.json").read_bytes())["records"]
    assert list(result.outcomes) == stored
    assert sum(r["provisional_label"] == 1 for r in result.outcomes) == 4
    assert all(
        r["actual_label_available_at"] is None and r["gold"] is False for r in result.outcomes
    )
    assert result.receipt["mode"] == "historical"
    assert (
        result.receipt["outcomes_sha256"]
        == hashlib.sha256(canonical({"records": stored}) + b"\n").hexdigest()
    )
    caps = result.receipt["capabilities"]
    assert caps["historical_data_readable"] is True
    assert all(
        caps[k] is False
        for k in [
            "current_cache_adoption",
            "source_authenticity_certified",
            "human_review_complete",
            "research_training_ready",
            "historical_replay",
            "candidate_release",
        ]
    )
    assets.verify_seal(result.receipt)


def test_original_files_and_acquisition_remain_exact_after_read(approved_original):
    audit, _, _, declaration = approved_original
    before = {p.name: p.read_bytes() for p in audit.iterdir()}
    result = consumer.verify_history(audit, "stage32-native-v1")
    assert {p.name: p.read_bytes() for p in audit.iterdir()} == before
    assert result.receipt["producer_execution_hash"] == content_hash(declaration["execution"])
    assert result.summary == json.loads(before["summary.json"])


@pytest.mark.parametrize(
    "member", ["declaration.json", "observations.jsonl", "outcomes.json", "summary.json"]
)
def test_every_original_payload_is_required(approved_original, member):
    audit, root, _, _ = approved_original
    target = copied(audit, root, "missing-" + member.replace(".", "-"))
    (target / member).unlink()
    with pytest.raises(consumer.ConsumptionError):
        consumer.verify_history(target, "stage32-native-v1")


def test_same_sealed_manifest_with_duplicate_json_key_is_refused(approved_original):
    audit, root, manifest, _ = approved_original
    target = copied(audit, root, "duplicate-key")
    raw = (target / "manifest.json").read_bytes()
    duplicate = (
        b'{"content_hash":'
        + json.dumps(manifest["content_hash"]).encode()
        + b","
        + raw.lstrip()[1:]
    )
    (target / "manifest.json").write_bytes(duplicate)
    with pytest.raises(consumer.ConsumptionError, match="JSON"):
        consumer.verify_history(target, "stage32-native-v1")


def test_extra_private_file_is_refused_without_deleting_it(approved_original):
    audit, root, _, _ = approved_original
    target = copied(audit, root, "extra")
    extra = target / "unexpected-secret.txt"
    extra.write_bytes(b"synthetic private marker")
    with pytest.raises(consumer.ConsumptionError):
        consumer.verify_history(target, "stage32-native-v1")
    assert extra.read_bytes() == b"synthetic private marker"


def test_resealed_label_edit_cannot_approve_itself(approved_original):
    audit, root, manifest, _ = approved_original
    target = copied(audit, root, "self-sealed")
    body = json.loads((target / "outcomes.json").read_bytes())
    next(row for row in body["records"] if row["provisional_label"] == 1)["provisional_label"] = 0
    (target / "outcomes.json").write_bytes(canonical(body) + b"\n")
    manifest["files"]["outcomes.json"] = assets.file_digest(target / "outcomes.json", lambda: None)
    manifest = assets.sealed({k: v for k, v in manifest.items() if k != "content_hash"})
    (target / "manifest.json").write_bytes(canonical(manifest) + b"\n")
    target.rename(target.with_name(content_hash(manifest)))
    forged = target.with_name(content_hash(manifest))
    with pytest.raises(consumer.ConsumptionError, match="APPROVAL|APPROVED"):
        consumer.verify_history(forged, "stage32-native-v1")


def test_cancellation_prevents_success_without_modifying_input(approved_original):
    audit, _, _, _ = approved_original

    def cancel():
        raise consumer.ConsumptionError("SOURCE_CONSUME_CANCELLED")

    before = (audit / "manifest.json").read_bytes()
    with pytest.raises(consumer.ConsumptionError, match="CANCELLED"):
        consumer.verify_history(audit, "stage32-native-v1", budget=consumer.Budget(tick=cancel))
    assert (audit / "manifest.json").read_bytes() == before


def test_current_missing_producer_files_are_safe_and_do_not_fallback(
    approved_original, monkeypatch
):
    audit, _, _, _ = approved_original

    def missing(*args, **kwargs):
        raise FileNotFoundError("/synthetic/private/token-marker")

    monkeypatch.setattr(producer, "verify_audit", missing)
    with pytest.raises(consumer.ConsumptionError) as error:
        consumer.verify_current(audit)
    assert error.value.code == "SOURCE_CURRENT_BINDING_UNAVAILABLE"
    assert "private" not in str(error.value) and "token-marker" not in str(error.value)


def test_cli_unknown_approval_has_safe_output():
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "packages.semantic.source_evidence_reader",
            "verify-history",
            "--approval",
            "unknown",
            "--artifact",
            "/synthetic/private/token-marker",
        ],
        cwd=assets.ROOT,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert b"SOURCE_CONSUME_NOT_APPROVED" in result.stdout + result.stderr
    assert (
        b"token-marker" not in result.stdout + result.stderr and b"Traceback" not in result.stderr
    )


def test_import_and_historical_read_start_no_subprocess_in_fresh_interpreter(approved_original):
    audit, _, _, _ = approved_original
    program = """import json,sys
attempts=[]
def deny(event,args):
 if event=='subprocess.Popen':
  attempts.append(event)
  raise AssertionError('Historical import/read attempted a subprocess')
sys.addaudithook(deny)
from packages.semantic import source_evidence_reader as reader
reader._APPROVALS=json.loads(sys.argv[2])
result=reader.verify_history(sys.argv[1],'stage32-native-v1')
assert not attempts
assert 'git' not in sys.modules and 'pydriller' not in sys.modules
assert 'packages.semantic.evaluation_population' not in sys.modules
assert 'packages.semantic.source_resolution' not in sys.modules
assert sum(r['provisional_label']==1 for r in result.outcomes)==4
print(json.dumps({'attempted_subprocesses':0,'positive_targets':4}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(audit), json.dumps(consumer._APPROVALS)],
        cwd=assets.ROOT,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert json.loads(result.stdout) == {"attempted_subprocesses": 0, "positive_targets": 4}


def test_catalog_cli_import_does_not_require_git_on_path():
    environment = os.environ.copy()
    environment["PATH"] = str(Path(sys.executable).parent)
    result = subprocess.run(
        [sys.executable, "-B", "-m", "packages.semantic.source_evidence_reader", "list-approved"],
        cwd=assets.ROOT,
        env=environment,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert len(json.loads(result.stdout)["approvals"]) == 2


@pytest.fixture
def parent_graph():
    generator = parent_cases.fixture.__wrapped__()
    value = next(generator)
    try:
        yield value
    finally:
        generator.close()


@pytest.mark.parametrize("case", ["consensus", "disagreement", "binary"])
def test_static_parent_dialect_retains_real_git_uncertainty(parent_graph, case):
    metadata, edges, declaration = parent_cases.actual_edges(
        parent_graph, disagree=case == "disagreement", binary=case == "binary"
    )
    result = V3.combine_edges(metadata, edges, declaration)
    V3.verify_resolution(result, declaration)
    outcomes = V1.outcomes(declaration, [result])
    if case == "consensus":
        assert result["positive_targets"] == [parent_graph["root"]]
        assert outcomes[0]["provisional_label"] == 1
    else:
        assert outcomes[0]["provisional_label"] is None
        if case == "disagreement":
            assert result["target_gaps"]
        else:
            assert result["global_gaps"] == ["binary"]
    assert metadata["parents"] == result["metadata"]["parents"]


def parent_unit(graph):
    metadata, edges, declaration = parent_cases.actual_edges(graph)
    reader = graph["reader"]("consumer-full-unit")
    full = []
    for edge in edges:
        files = reader.files(metadata["commit_id"], [edge["parent_sha"]])
        for file in files:
            file["diff_hash"] = hashlib.sha256((file.get("diff_text") or "").encode()).hexdigest()
        full.append(edge | {"files": files, "read_failure": None})
    result = V3.combine_edges(metadata, full, declaration)
    value = {
        "version": V3.VERSION,
        "raw_commit_hex": reader.command(["cat-file", "commit", metadata["commit_id"]]).hex(),
        "resolution": result,
        "assessment": fix_rules.evaluate(
            "fix error", "parsed", [file for edge in full for file in edge["files"]], []
        ),
    }
    return value, declaration


def test_static_full_parent_unit_verifies_raw_git_object_and_line_witnesses(parent_graph):
    value, declaration = parent_unit(parent_graph)
    V3.verify_unit(value, declaration)
    assert value["resolution"]["positive_targets"] == [parent_graph["root"]]


@pytest.mark.parametrize(
    "case", ["raw", "parent", "line_hash", "diff_hash", "target_parse", "assessment"]
)
def test_static_full_parent_unit_rejects_independent_fact_corruptions(parent_graph, case):
    value, declaration = parent_unit(parent_graph)
    if case == "raw":
        value["raw_commit_hex"] = "00"
    elif case == "parent":
        value["resolution"]["edges"].pop()
    elif case == "line_hash":
        value["resolution"]["edges"][0]["observation"]["trace"]["links"][0]["line_hash"] = "x" * 64
    elif case == "diff_hash":
        value["resolution"]["edges"][0]["files"][0]["diff_hash"] = "0" * 64
    elif case == "target_parse":
        value["resolution"]["target_evidence"][parent_graph["root"]]["parse_status"] = "unavailable"
    else:
        value["assessment"]["rule_candidate"] = False
    with pytest.raises(SemanticError):
        V3.verify_unit(value, declaration)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e100000"])
def test_nonfinite_manifest_numbers_are_refused_before_approval(approved_original, number):
    audit, root, _, _ = approved_original
    target = copied(audit, root, "number-" + number.replace("-", "minus"))
    raw = (target / "manifest.json").read_bytes().lstrip()
    (target / "manifest.json").write_bytes(b'{"invalid_number":' + number.encode() + b"," + raw[1:])
    with pytest.raises(consumer.ConsumptionError, match="JSON_NUMBER"):
        consumer.verify_history(target, "stage32-native-v1")


def test_declared_size_mismatch_is_rejected_before_hashing_payload(approved_original, monkeypatch):
    audit, root, _, _ = approved_original
    target = copied(audit, root, "size-mismatch")
    path = target / "observations.jsonl"
    with path.open("wb") as stream:
        stream.truncate(consumer.MAX_FILE + 1)
    original_record = consumer.data_io.file_record

    def record(path, maximum, budget):
        assert Path(path).name != "observations.jsonl", "Oversize payload must not be hashed"
        return original_record(path, maximum, budget)

    monkeypatch.setattr(consumer.data_io, "file_record", record)
    with pytest.raises(consumer.ConsumptionError, match="BYTE_LIMIT"):
        consumer.verify_history(target, "stage32-native-v1")


def test_hardlinked_payload_is_refused_and_original_is_preserved(approved_original):
    audit, root, _, _ = approved_original
    target = copied(audit, root, "hardlinked")
    shared = target / "summary.json"
    shared.unlink()
    before = (audit / "summary.json").read_bytes()
    os.link(audit / "summary.json", shared)
    try:
        with pytest.raises(consumer.ConsumptionError, match="FILE"):
            consumer.verify_history(target, "stage32-native-v1")
        assert (audit / "summary.json").read_bytes() == before
    finally:
        shared.unlink()


def test_input_change_after_semantic_read_refuses_terminal_success(approved_original, monkeypatch):
    audit, root, _, _ = approved_original
    target = copied(audit, root, "terminal-drift")
    original_read = consumer.data_io.read_json

    def read(path, *args, **kwargs):
        result = original_read(path, *args, **kwargs)
        if Path(path) == target / "summary.json":
            with (target / "outcomes.json").open("ab") as stream:
                stream.write(b" ")
        return result

    monkeypatch.setattr(consumer.data_io, "read_json", read)
    with pytest.raises(consumer.ConsumptionError, match="DRIFT"):
        consumer.verify_history(target, "stage32-native-v1")
    assert (target / "outcomes.json").read_bytes().endswith(b" ")


def test_catalog_change_during_read_cannot_publish(approved_original):
    audit, _, _, _ = approved_original
    changed = False

    def mutate():
        nonlocal changed
        if not changed:
            changed = True
            consumer._APPROVALS["stage32-native-v1"]["approval_scope"] += " changed"

    with pytest.raises(consumer.ConsumptionError, match="EXECUTION_DRIFT"):
        consumer.verify_history(audit, "stage32-native-v1", budget=consumer.Budget(tick=mutate))


def test_expired_budget_refuses_before_data_read(approved_original):
    audit, _, _, _ = approved_original
    budget = consumer.Budget()
    budget.deadline = time.monotonic() - 1
    with pytest.raises(consumer.ConsumptionError, match="DEADLINE"):
        consumer.verify_history(audit, "stage32-native-v1", budget=budget)


def test_sampled_memory_refusal_does_not_modify_input(approved_original, monkeypatch):
    audit, _, _, _ = approved_original
    before = (audit / "manifest.json").read_bytes()
    monkeypatch.setattr(consumer, "MAX_RSS", 1)
    with pytest.raises(consumer.ConsumptionError, match="MEMORY"):
        consumer.verify_history(audit, "stage32-native-v1")
    assert (audit / "manifest.json").read_bytes() == before


def test_returned_catalog_metadata_cannot_change_approval():
    before = content_hash(consumer._APPROVALS)
    records = consumer.approved_records()
    records[0]["manifest_hash"] = "0" * 64
    records[0]["model_qualification"] = True
    assert content_hash(consumer._APPROVALS) == before


@pytest.mark.parametrize("on_windows", [False, True])
def test_historical_junit_platform_is_taken_from_approved_producer(on_windows):
    root, _ = parent_cases.measurement_junit(on_windows)
    raw = ET.tostring(root)
    result = Measurement.junit_result(raw, on_windows=on_windows)
    assert result["passed"] == len(Measurement.REQUIRED_CASES) - int(on_windows)
    assert len(result["skipped"]) == int(on_windows)
    with pytest.raises(SemanticError):
        Measurement.junit_result(raw, on_windows=not on_windows)


@pytest.mark.parametrize("case", ["missing", "duplicate", "failure", "summary", "entity"])
def test_historical_safety_rejects_incomplete_or_unsafe_junit(case):
    root, suite = parent_cases.measurement_junit(True)
    if case == "missing":
        suite.remove(suite[0])
        suite.set("tests", str(len(suite)))
    elif case == "duplicate":
        suite.append(copy.deepcopy(suite[0]))
        suite.set("tests", str(len(suite)))
    elif case == "failure":
        ET.SubElement(suite[0], "failure")
    elif case == "summary":
        suite.set("errors", "1")
    raw = ET.tostring(root)
    if case == "entity":
        raw = b'<!DOCTYPE x [<!ENTITY x "expanded">]>' + raw
    with pytest.raises(SemanticError):
        Measurement.junit_result(raw, on_windows=True)


@pytest.mark.parametrize("seconds,enabled", [(8.0, True), (8.01, False), (12.0, False)])
def test_historical_cache_decision_preserves_negative_and_boundary(seconds, enabled):
    result = Measurement.decision(parent_cases.measurement_runs(seconds))
    assert result["cache_enabled"] is enabled
    assert result["reference_median_seconds"] == 10
    assert result["cached_median_seconds"] == seconds


@pytest.mark.parametrize("case", ["partial", "order", "nan", "cleanup", "equality", "commands"])
def test_historical_cache_cannot_be_adopted_from_fast_but_invalid_runs(case):
    runs = parent_cases.measurement_runs(1.0)
    if case == "partial":
        runs.pop()
    elif case == "order":
        runs.reverse()
    elif case == "nan":
        runs[0]["seconds"] = float("nan")
    elif case == "cleanup":
        runs[0]["cleanup_complete"] = False
    elif case == "equality":
        runs[0]["complete_original_observations_equal"] = False
    else:
        runs[0]["commands"]["cleanup_confirmed"] = 0
    with pytest.raises(SemanticError):
        Measurement.decision(runs)


def test_binary_payload_growth_cannot_trigger_an_unbounded_read(approved_original, monkeypatch):
    audit, root, _, _ = approved_original
    target = copied(audit, root, "binary-growth")
    artifact = consumer._Artifact(
        target, consumer.Budget(), consumer._APPROVALS["stage32-native-v1"]
    )
    path = target / "outcomes.json"
    size = path.stat().st_size
    original_open = Path.open
    requests = []

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, amount=-1):
            requests.append(amount)
            assert 0 < amount <= size + 1, "Data reads must retain their verified allocation bound"
            with original_open(path, "ab") as writer:
                writer.write(b" " * (1024 * 1024))
            return self.stream.read(amount)

    def opening(value, *args, **kwargs):
        stream = original_open(value, *args, **kwargs)
        return GrowingStream(stream) if value == path and args == ("rb",) else stream

    monkeypatch.setattr(Path, "open", opening)
    with pytest.raises(consumer.ConsumptionError, match="DRIFT"):
        artifact.binary("outcomes.json", consumer.MAX_RECORD)
    assert requests == [size + 1]
