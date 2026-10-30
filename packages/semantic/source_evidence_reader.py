"""Approved historical data reading; never a producer or cache/model authority."""

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import psutil

from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical, content_hash
from packages.semantic import asset_reader as data_io
from packages.semantic import native_runtime
from packages.semantic.policy import SemanticError
from packages.semantic.source_evidence_contracts import V1 as original
from packages.semantic.source_evidence_contracts import V3 as source
from packages.semantic.source_evidence_contracts import Measurement as measurement

VERSION = "native-source-consumption-v1"
MAX_ENTRIES = 64
MAX_BYTES = 128 * 1024**2
MAX_FILE = 64 * 1024**2
MAX_RECORD = 16 * 1024**2
MAX_MANIFEST = 2 * 1024**2
MAX_RSS = 1024**3
SOURCE_FILES = (
    "packages/semantic/source_evidence_reader.py",
    "packages/semantic/source_evidence_contracts.py",
    "packages/semantic/asset_reader.py",
    "packages/semantic/native_runtime.py",
    "packages/semantic/policy.py",
    "packages/repositories/safety.py",
    "packages/mining/fix_rules.py",
    "packages/mining/public_assets.py",
    "packages/mining/public_dataset.py",
    "packages/mining/source_evidence.py",
    "packages/mining/source_catalog.py",
    "uv.lock",
)
_BASIS = "cfb52c83f9bb2864840f47a86845e06f777149f854afce1caf4824bc58c0e2e8"
_APPROVALS = {
    "stage32-native-v1": {
        "approval_scope": "technical sealed historical source reading only",
        "manifest_hash": "1731b72a6b269e87a09116bb676e56aa688e9ef8eb7da5fc1f35093233c3a1f8",
        "directory_id": "ed92fa6d4e2d302d321eb93162e59b93d0eda104ac52f518698081ec5fbeb6da",
        "producer_version": "native-message-szz-horizon-source-v1-audit",
        "producer_sources_hash": "d644210cde139f0ae5cfae01a99690f6e3dd96862a125f928dd31326fc3d68e9",
        "producer_execution_hash": (
            "85e3787b02caa5dcbea5ed357154e25073c1526ff02de5379f18122fa0a7a1c3"
        ),
        "strict_independent_replay_verified": True,
        "model_qualification": False,
        "human_review_complete": False,
        "approval_basis_stage33_final_delivery_sha256": _BASIS,
    },
    "stage33-source6-v3": {
        "approval_scope": "technical sealed historical source reading only",
        "manifest_hash": "7ad80a95c9a219d0d67be5634c2e7b3d6dcaa653a3e3ef0ae990d703d00330d0",
        "directory_id": "44f8b6e6329923003d50d69db6f69318ac8810b97a0465cf2082e4102f7a6537",
        "producer_version": "native-parent-consensus-source-v3-audit",
        "producer_sources_hash": "ea61a701f367a064dcbf9eea1261bb9adfdb344829b1b7dc0cfea4e21c99af65",
        "producer_execution_hash": (
            "9a5432bd8d6aff3bbe44f12ad3265eb7b5f35ad27468d6726e5ff5f671950b00"
        ),
        "strict_independent_replay_verified": True,
        "model_qualification": False,
        "human_review_complete": False,
        "approval_basis_stage33_final_delivery_sha256": _BASIS,
        "benchmark_manifest_hash": (
            "3ab6610a3e540ae1ebc6fe572fe64b39bb846e71bfad7ba635666fbd8721d8c4"
        ),
        "historical_safety_manifest_hash": (
            "cc85ce74bad5aa1f17d17a72cbcffdb66aa037e16ef736a28fe8538a304c27aa"
        ),
        "historical_test_sources_hash": (
            "b928ce069d9f60b9ca44be93b359f0f31c56652075cf9ab2d1d58ce5fcef9a18"
        ),
        "historical_test_identities_hash": (
            "8a9c3009bbcf6604b4fdaf18c37659a4af907147f4ebf2ec483b5b11a3194be2"
        ),
        "historical_safety_collected": 93,
        "historical_platform": {"os_name": "nt", "sys_platform": "win32"},
        "original_approval": "stage32-native-v1",
        "original_manifest_hash": (
            "1731b72a6b269e87a09116bb676e56aa688e9ef8eb7da5fc1f35093233c3a1f8"
        ),
        "original_execution_hash": (
            "85e3787b02caa5dcbea5ed357154e25073c1526ff02de5379f18122fa0a7a1c3"
        ),
    },
}
_QUALIFICATIONS = (
    "human_review_complete",
    "native14_features_verified",
    "new_representation_verified",
    "contamination_certified",
    "research_training_ready",
    "historical_replay",
    "candidate_release",
)


class ConsumptionError(data_io.ConsumptionError):
    def __init__(self, code="SOURCE_CONSUME_INVALID", status=409):
        super().__init__(code, status)


def require(condition, code="SOURCE_CONSUME_INVALID"):
    if not condition:
        raise ConsumptionError(code)


def _guarded(action):
    try:
        return action()
    except ConsumptionError:
        raise
    except data_io.ConsumptionError as error:
        raise ConsumptionError(error.code.replace("SEMANTIC_ASSET_", "SOURCE_CONSUME_")) from error
    except SemanticError as error:
        code = str(error)
        raise ConsumptionError(
            code if re.fullmatch(r"[A-Z0-9_]{1,96}", code) else "SOURCE_CONSUME_INVALID"
        ) from error
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise ConsumptionError("SOURCE_CONSUME_INVALID") from error


class Budget(data_io.Budget):
    def __init__(self, tick=native_runtime.checkpoint, seconds=60):
        super().__init__(tick=tick, seconds=seconds)
        self.entries = self.payload_bytes = 0
        self._sample_at = 0.0

    def check(self):
        super().check()
        now = time.monotonic()
        if now >= self._sample_at:
            try:
                require(psutil.Process().memory_info().rss <= MAX_RSS, "SOURCE_CONSUME_MEMORY")
            except psutil.Error as error:
                raise ConsumptionError("SOURCE_CONSUME_OBSERVATION_FAILED") from error
            self._sample_at = now + 0.02


def _safe_file(path):
    path = data_io.safe(path)
    value = path.stat()
    require(stat.S_ISREG(value.st_mode) and value.st_nlink == 1, "SOURCE_CONSUME_FILE")
    return path


def _parse(raw):
    return json.loads(
        raw,
        object_pairs_hook=data_io.pairs,
        parse_float=data_io.finite_float,
        parse_constant=lambda _: require(False, "SOURCE_CONSUME_JSON_NUMBER"),
    )


class _Artifact:
    def __init__(self, directory, budget, approval=None):
        self.directory, self.budget = data_io.safe(directory), budget
        require(self.directory.is_dir(), "SOURCE_CONSUME_PATH")
        budget.check()
        names = []
        for entry in self.directory.iterdir():
            budget.check()
            names.append(entry.name)
            require(len(names) <= MAX_ENTRIES, "SOURCE_CONSUME_ENTRIES")
            _safe_file(entry)
        budget.entries += len(names) + 1
        require(budget.entries <= MAX_ENTRIES, "SOURCE_CONSUME_ENTRIES")
        manifest_path = _safe_file(self.directory / "manifest.json")
        self.manifest = data_io.read_json(manifest_path, MAX_MANIFEST, budget)
        assets.verify_seal(self.manifest)
        if approval is not None:
            require(
                self.manifest["content_hash"] == approval["manifest_hash"]
                and self.manifest["version"] == approval["producer_version"]
                and content_hash(self.manifest["sources"]) == approval["producer_sources_hash"],
                "SOURCE_CONSUME_APPROVAL_MISMATCH",
            )
        require(self.directory.name == content_hash(self.manifest), "SOURCE_CONSUME_IDENTITY")
        if approval is not None:
            require(
                self.directory.name == approval["directory_id"], "SOURCE_CONSUME_APPROVAL_MISMATCH"
            )
        files = self.manifest["files"]
        require(isinstance(files, dict) and 0 < len(files) < MAX_ENTRIES)
        require(set(names) == {"manifest.json", *files}, "SOURCE_CONSUME_INVENTORY")
        for name, record in files.items():
            require(
                isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name),
                "SOURCE_CONSUME_PATH",
            )
            require(name != "manifest.json", "SOURCE_CONSUME_INVENTORY")
            data_io.descriptor(record, MAX_FILE)
            require(
                _safe_file(self.directory / name).stat().st_size == record["bytes"],
                "SOURCE_CONSUME_BYTE_LIMIT",
            )
        budget.payload_bytes += sum(record["bytes"] for record in files.values())
        require(budget.payload_bytes <= MAX_BYTES, "SOURCE_CONSUME_BYTE_LIMIT")
        self._manifest_record = data_io.file_record(manifest_path, MAX_MANIFEST, budget)
        self._stamps = {name: data_io.stamp(self.directory / name) for name in names}
        self.verify_unchanged()

    def json(self, name):
        require(name in self.manifest["files"], "SOURCE_CONSUME_INVENTORY")
        return data_io.read_json(
            self.directory / name, MAX_RECORD, self.budget, self.manifest["files"][name]
        )

    def binary(self, name, maximum):
        require(name in self.manifest["files"], "SOURCE_CONSUME_INVENTORY")
        expected = self.manifest["files"][name]
        data_io.descriptor(expected, maximum)
        path = _safe_file(self.directory / name)
        before = data_io.stamp(path)
        require(before[2] == expected["bytes"], "SOURCE_CONSUME_DRIFT")
        self.budget.check()
        with path.open("rb") as stream:
            require(data_io.opened_stamp(stream) == before, "SOURCE_CONSUME_DRIFT")
            raw = stream.read(before[2] + 1)
            require(data_io.opened_stamp(stream) == before, "SOURCE_CONSUME_DRIFT")
        require(
            len(raw) == before[2]
            and data_io.stamp(path) == before
            and hashlib.sha256(raw).hexdigest() == expected["sha256"],
            "SOURCE_CONSUME_DRIFT",
        )
        self.budget.check()
        return raw

    def rows(self, name):
        require(name in self.manifest["files"], "SOURCE_CONSUME_INVENTORY")
        path = _safe_file(self.directory / name)
        before = data_io.stamp(path)
        result = []
        with path.open("rb") as stream:
            require(data_io.opened_stamp(stream) == before, "SOURCE_CONSUME_DRIFT")
            while raw := stream.readline(MAX_RECORD + 1):
                self.budget.check()
                require(
                    len(raw) <= MAX_RECORD and len(result) < original.MAX_OBSERVATIONS,
                    "SOURCE_CONSUME_RECORD_LIMIT",
                )
                result.append(_parse(raw))
            require(data_io.opened_stamp(stream) == before, "SOURCE_CONSUME_DRIFT")
        require(data_io.stamp(path) == before, "SOURCE_CONSUME_DRIFT")
        return result

    def verify_unchanged(self):
        self.budget.check()
        require(
            set(p.name for p in self.directory.iterdir()) == set(self._stamps),
            "SOURCE_CONSUME_DRIFT",
        )
        require(
            data_io.file_record(self.directory / "manifest.json", MAX_MANIFEST, self.budget)
            == self._manifest_record,
            "SOURCE_CONSUME_DRIFT",
        )
        for name, expected in self.manifest["files"].items():
            self.budget.check()
            _safe_file(self.directory / name)
            require(
                data_io.stamp(self.directory / name) == self._stamps[name], "SOURCE_CONSUME_DRIFT"
            )
            require(
                data_io.file_record(self.directory / name, MAX_FILE, self.budget) == expected,
                "SOURCE_CONSUME_DRIFT",
            )


def approved_records():
    return [
        {
            "approval_id": key,
            "scope": value["approval_scope"],
            "manifest_hash": value["manifest_hash"],
            "producer_version": value["producer_version"],
            "model_qualification": False,
            "human_review_complete": False,
        }
        for key, value in sorted(_APPROVALS.items())
    ]


def _execution(value, approval):
    require(
        isinstance(value, dict) and set(value) == {"sources", "git_version", "python"},
        "SOURCE_CONSUME_PRODUCER",
    )
    require(content_hash(value) == approval["producer_execution_hash"], "SOURCE_CONSUME_PRODUCER")
    require(
        content_hash(value["sources"]) == approval["producer_sources_hash"],
        "SOURCE_CONSUME_PRODUCER",
    )
    require(isinstance(value["git_version"], str) and 0 < len(value["git_version"]) <= 160)
    require(
        isinstance(value["python"], list)
        and len(value["python"]) == 3
        and all(type(v) is int and v >= 0 for v in value["python"])
    )
    require(
        isinstance(value["sources"], dict)
        and all(isinstance(n, str) and data_io.digest_id(h) for n, h in value["sources"].items())
    )


def _original_declaration(value, approval, budget):
    assets.verify_seal(value)
    require(value["version"] == original.VERSION, "SOURCE_CONSUME_VERSION")
    _execution(value["execution"], approval)
    require(
        isinstance(value["targets"], list) and 0 < len(value["targets"]) <= original.MAX_TARGETS
    )
    require(
        isinstance(value["observations"], list)
        and len(value["observations"]) <= original.MAX_OBSERVATIONS
    )
    require(
        isinstance(value["reachable_metadata"], dict) and len(value["reachable_metadata"]) <= 32768
    )
    for key, row in value["reachable_metadata"].items():
        budget.check()
        require(
            key == row["commit_id"] and isinstance(key, str) and re.fullmatch(r"[0-9a-f]{40}", key),
            "SOURCE_CONSUME_IDENTITY",
        )
        require(
            set(row) == {"commit_id", "committer_epoch", "parents"}
            and type(row["committer_epoch"]) is int
        )
        require(
            isinstance(row["parents"], list)
            and all(isinstance(p, str) and re.fullmatch(r"[0-9a-f]{40}", p) for p in row["parents"])
        )
    observations = value["observations"]
    require(
        all(
            set(row) == {"commit_id", "committer_epoch", "author_epoch", "parents", "message_hash"}
            and type(row["author_epoch"]) is int
            and data_io.digest_id(row["message_hash"])
            for row in observations
        ),
        "SOURCE_CONSUME_OBSERVATIONS",
    )
    require(
        len({r["commit_id"] for r in observations}) == len(observations),
        "SOURCE_CONSUME_OBSERVATIONS",
    )
    selected = [
        r | {"role": original.role(r["committer_epoch"])}
        for r in observations
        if original.START <= r["committer_epoch"] < original.TARGET_STOP
        and len(r["parents"]) == 1
        and r["commit_id"] not in set(value["excluded_source_ids"])
    ]
    require(
        selected == value["targets"]
        and value["horizon_seconds"] == original.HORIZON
        and value["hypothetical_lag_seconds"] == original.LAG,
        "SOURCE_CONSUME_ROLE",
    )
    expected = sorted(
        (
            r
            for r in value["reachable_metadata"].values()
            if original.START <= r["committer_epoch"] < original.STOP
        ),
        key=lambda r: (r["committer_epoch"], r["commit_id"]),
    )
    require(
        len(expected) == len(observations)
        and all(
            all(actual[k] == v for k, v in item.items())
            for item, actual in zip(expected, observations, strict=True)
        ),
        "SOURCE_CONSUME_OBSERVATIONS",
    )
    require(
        set(value["reserved_identities"])
        <= {r["commit_id"] for r in value["targets"] if r["role"] == original.ROLES[3]},
        "SOURCE_CONSUME_RESERVATION",
    )
    require(
        value["issue_observations"] is False
        and value["qualification_promoted"] is False
        and value["label_kind"] == "provisional_szz_silver",
        "SOURCE_CONSUME_QUALIFICATION",
    )
    assets.verify_seal(value["source_receipt"])


def _support(rows):
    return {
        role: dict(
            Counter(
                "unknown" if r["provisional_label"] is None else str(r["provisional_label"])
                for r in rows
                if r["role"] == role
            )
        )
        for role in original.ROLES
    }


def _derive(declaration, rows, budget):
    # The known policy bounds the projection to at most1200x2048 comparisons.
    budget.check()
    result = original.outcomes(declaration, rows)
    budget.check()
    return result


@dataclass(frozen=True)
class VerifiedSource:
    manifest: dict
    summary: dict
    outcomes: tuple
    receipt: dict


def _verify_original(artifact, approval):
    manifest, budget = artifact.manifest, artifact.budget
    require(
        set(manifest["files"])
        == {"declaration.json", "observations.jsonl", "outcomes.json", "summary.json"},
        "SOURCE_CONSUME_INVENTORY",
    )
    declaration = artifact.json("declaration.json")
    _original_declaration(declaration, approval, budget)
    require(
        manifest["declaration_hash"] == declaration["content_hash"]
        and manifest["sources"] == declaration["execution"]["sources"]
    )
    rows = artifact.rows("observations.jsonl")
    require(
        [r["metadata"] for r in rows] == declaration["observations"], "SOURCE_CONSUME_OBSERVATIONS"
    )
    for row in rows:
        budget.check()
        original.verify_observation(row, declaration)
    expected = _derive(declaration, rows, budget)
    require(artifact.json("outcomes.json") == {"records": expected}, "SOURCE_CONSUME_OUTCOMES")
    summary = artifact.json("summary.json")
    support = _support(expected)
    required = {
        "version": original.VERSION,
        "complete_population": True,
        "planned_observations": len(declaration["observations"]),
        "completed_observations": len(rows),
        "targets": len(expected),
        "support": support,
        "calibration_positive_min10": support[original.ROLES[1]].get("1", 0) >= 10,
        "threshold_positive_min10": support[original.ROLES[2]].get("1", 0) >= 10,
        "global_gap_observations": sum(bool(r["global_gaps"]) for r in rows),
        "label_kind": "provisional_szz_silver",
        **{k: False for k in _QUALIFICATIONS},
    }
    require(
        all(
            summary[k] == value and (type(summary[k]) is bool if type(value) is bool else True)
            for k, value in required.items()
        ),
        "SOURCE_CONSUME_SUMMARY",
    )
    for key in ("units_created", "prior_completed_units_reused", "cache_hits_total"):
        require(type(summary[key]) is int and summary[key] >= 0, "SOURCE_CONSUME_SUMMARY")
    return declaration, rows, expected, summary


def _consumer_binding():
    return {
        "sources": {
            name: hashlib.sha256(
                (assets.ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            for name in SOURCE_FILES
        },
        "python": list(sys.version_info[:3]),
        "platform": {"os_name": os.name, "sys_platform": sys.platform},
    }


def _subset(artifact, manifest, prefix, names):
    assets.verify_seal(manifest)
    require(set(manifest["files"]) == set(names), "SOURCE_CONSUME_NESTED_INVENTORY")
    require(
        all(
            artifact.manifest["files"].get(prefix + name) == descriptor
            for name, descriptor in manifest["files"].items()
        ),
        "SOURCE_CONSUME_NESTED_BINDING",
    )


def _historical_safety(artifact, manifest, approval, prefix, execution):
    _subset(artifact, manifest, prefix, measurement.SAFETY_FILES)
    require(
        manifest["content_hash"] == approval["historical_safety_manifest_hash"]
        and manifest["version"] == source.VERSION + "-runtime-safety"
        and manifest["sources"] == execution["sources"],
        "SOURCE_CONSUME_SAFETY_BINDING",
    )
    value = artifact.json(prefix + "safety.json")
    require(
        value["version"] == manifest["version"]
        and value["execution"] == execution
        and value["platform"] == approval["historical_platform"]
        and content_hash(value["test_sources"]) == approval["historical_test_sources_hash"]
        and set(value["test_sources"]) == {*execution["sources"], *measurement.TEST_FILES}
        and all(
            value["test_sources"][key] == digest for key, digest in execution["sources"].items()
        ),
        "SOURCE_CONSUME_SAFETY_BINDING",
    )
    raw = artifact.binary(prefix + "junit.xml", MAX_RECORD)
    actual = measurement.junit_result(
        raw, on_windows=approval["historical_platform"]["os_name"] == "nt"
    )
    require(
        actual == value["test_result"]
        and actual["collected"] == approval["historical_safety_collected"]
        and content_hash(actual["identities"]) == approval["historical_test_identities_hash"],
        "SOURCE_CONSUME_SAFETY_POLICY",
    )


def _historical_benchmark(artifact, manifest, approval, declaration, origin, old_rows):
    prefix = "cache-benchmark-"
    names = {
        *measurement.SAFETY_FILES,
        "resolution.json",
        "original-observations.jsonl",
        "benchmark.json",
    }
    for index in range(1, 7):
        names.update({f"run-{index:02}-start.json", f"run-{index:02}.json"})
        names.update(measurement._unit_name(index, unit) for unit in (1, 2))
    _subset(artifact, manifest, prefix, names)
    require(
        manifest["version"] == source.VERSION + "-cache-benchmark"
        and manifest["content_hash"] == approval["benchmark_manifest_hash"]
        and manifest["sources"] == declaration["execution"]["sources"],
        "SOURCE_CONSUME_BENCHMARK_BINDING",
    )
    require(artifact.json(prefix + "resolution.json") == declaration, "SOURCE_CONSUME_DECLARATION")
    value = artifact.json(prefix + "benchmark.json")
    require(
        value["version"] == manifest["version"]
        and value["declaration_hash"] == declaration["content_hash"]
        and value["original_target_eligibility_frozen"] is True
        and value["qualification_promoted"] is False
        and value["platform"] == approval["historical_platform"]
        and manifest["files"]["original-observations.jsonl"]
        == origin.manifest["files"]["observations.jsonl"],
        "SOURCE_CONSUME_BENCHMARK_BINDING",
    )
    _historical_safety(
        artifact, value["safety_manifest"], approval, prefix, declaration["execution"]
    )
    by_id = {row["metadata"]["commit_id"]: row for row in old_rows}
    runs = []
    for index, (pair, cached) in enumerate(measurement.run_order(), 1):
        artifact.budget.check()
        require(
            artifact.json(prefix + f"run-{index:02}-start.json")
            == {"index": index, "pair": pair, "cached": cached},
            "SOURCE_CONSUME_BENCHMARK_ORDER",
        )
        row = artifact.json(prefix + f"run-{index:02}.json")
        hashes = []
        for unit, sha in enumerate(
            declaration["input_plan"]["benchmark_completed_observation_ids"], 1
        ):
            actual = artifact.json(prefix + measurement._unit_name(index, unit))
            require(actual == by_id[sha], "SOURCE_CONSUME_BENCHMARK_UNIT")
            hashes.append(hashlib.sha256(canonical(actual)).hexdigest())
        require(row["observation_hashes"] == hashes, "SOURCE_CONSUME_BENCHMARK_UNIT")
        runs.append(row)
    require(value["decision"] == measurement.decision(runs), "SOURCE_CONSUME_BENCHMARK_DECISION")
    return value


def _verify_parent_audit(artifact, approval, origin, origin_approval):
    budget, manifest = artifact.budget, artifact.manifest
    old, old_rows, _, _ = _verify_original(origin, origin_approval)
    require(
        origin.manifest["content_hash"] == approval["original_manifest_hash"],
        "SOURCE_CONSUME_ORIGIN_BINDING",
    )
    require(
        content_hash(old["execution"]) == approval["original_execution_hash"],
        "SOURCE_CONSUME_ORIGIN_BINDING",
    )
    value = artifact.json("resolution.json")
    assets.verify_seal(value)
    _execution(value["execution"], approval)
    require(
        value["version"] == source.VERSION
        and value["cache_default"] is False
        and value["qualification_promoted"] is False
        and value["original"] == old
        and value["original_manifest"] == origin.manifest
        and value["content_hash"] == manifest["declaration_hash"]
        and manifest["sources"] == value["execution"]["sources"]
        and value["input_plan"]["original_source_manifest"] == origin.manifest["content_hash"]
        and manifest["files"]["original-observations.jsonl"]
        == origin.manifest["files"]["observations.jsonl"],
        "SOURCE_CONSUME_ORIGIN_BINDING",
    )
    source._plan_matches(value["input_plan"], old, old_rows)
    mode = artifact.json("cache-mode.json")
    require(set(mode) == {"enabled", "benchmark_manifest"} and type(mode["enabled"]) is bool)
    extras = set()
    if mode["benchmark_manifest"] is not None:
        benchmark = _historical_benchmark(
            artifact, mode["benchmark_manifest"], approval, value, origin, old_rows
        )
        require(
            mode["enabled"] is benchmark["decision"]["cache_enabled"], "SOURCE_CONSUME_CACHE_RECORD"
        )
        extras = {"cache-benchmark-" + name for name in mode["benchmark_manifest"]["files"]}
    else:
        require(
            approval.get("benchmark_manifest_hash") is None and mode["enabled"] is False,
            "SOURCE_CONSUME_CACHE_RECORD",
        )
    require(
        set(manifest["files"])
        == {
            "resolution.json",
            "original-observations.jsonl",
            "observations.jsonl",
            "outcomes.json",
            "summary.json",
            "cache-mode.json",
            *extras,
        },
        "SOURCE_CONSUME_INVENTORY",
    )
    rows = artifact.rows("observations.jsonl")
    require(
        [source._projection(row)["metadata"] for row in rows] == old["observations"],
        "SOURCE_CONSUME_OBSERVATIONS",
    )
    requested = set(value["input_plan"]["all_gap_observations"])
    by_id = {row["metadata"]["commit_id"]: row for row in old_rows}
    for row in rows:
        budget.check()
        sha = source._projection(row)["metadata"]["commit_id"]
        if sha in requested:
            require(row["kind"] == "resolved", "SOURCE_CONSUME_GAP_NOT_ATTEMPTED")
            source.verify_unit(row["value"], old)
        else:
            require(
                row == {"kind": "borrowed", "value": by_id[sha]}, "SOURCE_CONSUME_BORROWED_DRIFT"
            )
            original.verify_observation(row["value"], old)
    outcomes = _derive(old, [source._projection(row) for row in rows], budget)
    require(artifact.json("outcomes.json") == {"records": outcomes}, "SOURCE_CONSUME_OUTCOMES")
    summary = artifact.json("summary.json")
    require(
        summary == source._summary(value, rows, outcomes, False, cache_enabled=mode["enabled"]),
        "SOURCE_CONSUME_SUMMARY",
    )
    return outcomes, summary


def _receipt(artifact, approval_id, approval, outcomes, summary, consumer_binding, origin=None):
    return assets.sealed(
        {
            "version": VERSION,
            "mode": "historical",
            "approval_id": approval_id,
            "producer_manifest": artifact.manifest["content_hash"],
            "origin_manifest": origin.manifest["content_hash"] if origin else None,
            "producer_execution_hash": approval["producer_execution_hash"],
            "producer_version": artifact.manifest["version"],
            "consumer": consumer_binding,
            "targets": len(outcomes),
            "observations": summary["completed_observations"],
            "support": summary["support"],
            "outcomes_sha256": hashlib.sha256(canonical({"records": outcomes}) + b"\n").hexdigest(),
            "historical_cache_enabled": summary.get("cache_enabled", False),
            "prior_strict_replay_recorded": approval["strict_independent_replay_verified"],
            "capabilities": {
                "historical_data_readable": True,
                "current_execution_verified": False,
                "current_cache_adoption": False,
                "source_authenticity_certified": False,
                **{key: False for key in _QUALIFICATIONS},
            },
        }
    )


def verify_history(directory, approval_id, *, origin=None, budget=None):
    def work():
        require(approval_id in _APPROVALS, "SOURCE_CONSUME_NOT_APPROVED")
        catalog_hash = content_hash(_APPROVALS)
        approval = copy.deepcopy(_APPROVALS[approval_id])
        if approval["producer_version"] == original.VERSION + "-audit":
            require(origin is None, "SOURCE_CONSUME_ORIGIN_UNEXPECTED")
        else:
            require(origin is not None, "SOURCE_CONSUME_ORIGIN_REQUIRED")
        active = budget or Budget()
        active.check()
        binding = _consumer_binding()
        artifact = _Artifact(Path(directory), active, approval)
        if approval["producer_version"] == original.VERSION + "-audit":
            require(origin is None, "SOURCE_CONSUME_ORIGIN_UNEXPECTED")
            _, _, outcomes, summary = _verify_original(artifact, approval)
            related = None
        else:
            require(
                approval["producer_version"] == source.VERSION + "-audit", "SOURCE_CONSUME_VERSION"
            )
            require(origin is not None, "SOURCE_CONSUME_ORIGIN_REQUIRED")
            origin_id = approval["original_approval"]
            require(origin_id in _APPROVALS, "SOURCE_CONSUME_NOT_APPROVED")
            origin_approval = copy.deepcopy(_APPROVALS[origin_id])
            related = _Artifact(Path(origin), active, origin_approval)
            require(
                not artifact.directory.is_relative_to(related.directory)
                and not related.directory.is_relative_to(artifact.directory),
                "SOURCE_CONSUME_ORIGIN_OVERLAP",
            )
            outcomes, summary = _verify_parent_audit(artifact, approval, related, origin_approval)
            related.verify_unchanged()
        artifact.verify_unchanged()
        require(
            catalog_hash == content_hash(_APPROVALS) and binding == _consumer_binding(),
            "SOURCE_CONSUME_EXECUTION_DRIFT",
        )
        active.check()
        receipt = _receipt(artifact, approval_id, approval, outcomes, summary, binding, related)
        return VerifiedSource(artifact.manifest, summary, tuple(outcomes), receipt)

    return _guarded(work)


def verify_current(directory, *, budget=None):
    def work():
        active = budget or Budget()
        artifact = _Artifact(Path(directory), active)
        version = artifact.manifest["version"]
        try:
            if version in {original.VERSION + "-audit", original.VERSION + "-pilot"}:
                from packages.semantic.evaluation_population import verify_audit as verify
            elif version in {source.VERSION + "-audit", source.VERSION + "-pilot"}:
                from packages.semantic.source_resolution import verify_audit as verify
            else:
                require(False, "SOURCE_CONSUME_VERSION")
            result = verify(artifact.directory, active.check)
        except (FileNotFoundError, ImportError, RuntimeError) as error:
            raise ConsumptionError("SOURCE_CURRENT_BINDING_UNAVAILABLE") from error
        artifact.verify_unchanged()
        active.check()
        return result

    return _guarded(work)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ConsumptionError("SOURCE_CONSUME_INVALID_ARGUMENT")


def main(arguments=None):
    try:
        parser = _Parser(prog="source-evidence")
        commands = parser.add_subparsers(dest="command", required=True)
        commands.add_parser("list-approved")
        history = commands.add_parser("verify-history")
        history.add_argument("--approval", required=True)
        history.add_argument("--artifact", required=True)
        history.add_argument("--origin")
        current = commands.add_parser("verify-current")
        current.add_argument("--artifact", required=True)
        request = parser.parse_args(arguments)
        if request.command == "list-approved":
            value = {"version": VERSION, "approvals": approved_records()}
        elif request.command == "verify-history":
            value = verify_history(
                request.artifact, request.approval, origin=request.origin
            ).receipt
        else:
            manifest, summary = verify_current(request.artifact)
            value = {
                "version": VERSION,
                "mode": "current",
                "manifest": manifest["content_hash"],
                "summary": summary,
                "new_qualification_granted": False,
            }
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except ConsumptionError as error:
        print(json.dumps({"error": error.code}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
