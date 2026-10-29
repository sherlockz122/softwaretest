"""Fixed new native source evidence; provisional outcomes never certify a model."""

import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import fix_rules, szz_engine
from packages.mining import public_assets as assets
from packages.mining.research_dataset import CSV_FIELDS, PARTITIONS
from packages.mining.source_evidence import canonical, content_hash
from packages.repositories.clone import terminate
from packages.repositories.parse_policy import METADATA_LIMIT
from packages.repositories.parser import NativeReader
from packages.repositories.safety import RepositoryError
from packages.semantic import artifacts
from packages.semantic.admission import commit_clocks
from packages.semantic.admission_git import object_hash, sha_valid
from packages.semantic.asset_lock import publication
from packages.semantic.control import checkpoint
from packages.semantic.git_inputs import native_environment
from packages.semantic.policy import SemanticError

VERSION = "native-message-szz-horizon-source-v1"
REPOSITORY = "apache/activemq"
START = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp())
TARGET_STOP = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
STOP = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp())
HORIZON = 90 * 86400
LAG = 7 * 86400
MAX_TARGETS, MAX_OBSERVATIONS = 1200, 2048
MAX_UNIT, MAX_FILES, MAX_TREE = 16 * 1024**2, 4096, 1024**3
SOURCES = (
    "packages/semantic/evaluation_population.py",
    "packages/semantic/evaluation_weights.py",
    "scripts/semantic_evaluation_audit.py",
    "packages/semantic/artifacts.py",
    "packages/semantic/asset_lock.py",
    "packages/semantic/asset_reader.py",
    "packages/semantic/admission.py",
    "packages/semantic/admission_git.py",
    "packages/semantic/git_inputs.py",
    "packages/semantic/control.py",
    "packages/semantic/policy.py",
    "packages/repositories/parser.py",
    "packages/repositories/parse_policy.py",
    "packages/repositories/clone.py",
    "packages/mining/fix_rules.py",
    "packages/mining/szz_engine.py",
    "packages/mining/szz.py",
    "packages/mining/public_assets.py",
    "packages/mining/source_evidence.py",
    "uv.lock",
)
ROLES = ("train_candidate", "calibration_candidate", "threshold_candidate", "evaluation_candidate")


def require(condition, code="EVAL_SOURCE_INVALID"):
    if not condition:
        raise SemanticError(code)


def epoch(value):
    return int(value.replace(tzinfo=UTC).timestamp())


def role(timestamp):
    year = datetime.fromtimestamp(timestamp, UTC).year
    return (
        ROLES[0] if 2020 <= year <= 2022 else ROLES[year - 2022] if 2023 <= year <= 2025 else None
    )


def _atomic(path, value):
    path = assets.safe_path(path)
    raw = canonical(value) + b"\n"
    require(len(raw) <= MAX_UNIT, "EVAL_UNIT_LIMIT")
    temporary = path.parent / (".unit-part-" + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            require(path.read_bytes() == raw, "EVAL_PUBLICATION_CONFLICT")
    finally:
        temporary.unlink(missing_ok=True)


def _execution():
    git = shutil.which("git")
    require(git is not None, "EVAL_GIT_MISSING")
    version = (
        subprocess.check_output([git, "--version"], stdin=subprocess.DEVNULL, timeout=10)
        .decode("ascii")
        .strip()
    )
    return {
        "sources": artifacts.sources(SOURCES),
        "git_version": version,
        "python": list(sys.version_info[:3]),
    }


def _inventory(source, check):
    source = assets.safe_path(Path(source))
    bare = source / "source.git"
    require(not (bare / "shallow").exists(), "EVAL_SHALLOW_SOURCE")
    for name in ("objects/info/alternates", "objects/info/http-alternates", "info/grafts"):
        require(
            not (bare / name).exists() and not (bare / name).is_symlink(), "EVAL_SOURCE_ALTERNATES"
        )
    result = {}
    total = 0
    for current, directories, files in os.walk(
        assets.safe_path(bare / "objects"), followlinks=False
    ):
        for name in directories:
            assets.safe_path(Path(current) / name)
        for name in files:
            check()
            path = assets.safe_path(Path(current) / name)
            require(not name.endswith(".promisor"), "EVAL_PROMISOR_SOURCE")
            value = assets.file_digest(path, check, maximum=256 * 1024**2)
            result[path.relative_to(source).as_posix()] = value
            total += value["bytes"]
            require(
                len(result) <= MAX_FILES and total <= 512 * 1024**2, "EVAL_SOURCE_INVENTORY_LIMIT"
            )
    for name in ("acquisition.json", "source.git/config"):
        result[name] = assets.file_digest(source / name, check, maximum=65536)
    return result


class Reader(NativeReader):
    """Verified objects, original bounds; POSIX Git stays in the supervised session."""

    def command(
        self, arguments, limit=METADATA_LIMIT, allow_limit=False, codes=(0,), exit_code=False
    ):
        require(
            arguments
            and arguments[0]
            in {"rev-parse", "rev-list", "cat-file", "diff-tree", "ls-tree", "blame", "merge-base"},
            "EVAL_GIT_COMMAND",
        )
        require(type(limit) is int and 0 < limit <= 8 * 1024**2, "EVAL_GIT_LIMIT")
        stdout, stderr = self.attempt / "read.out", self.attempt / "read.err"
        process = None
        try:
            with stdout.open("wb") as output, stderr.open("wb") as errors:
                process = subprocess.Popen(
                    [self.git, "--literal-pathspecs", "-C", str(self.bare), *arguments],
                    cwd=self.attempt,
                    env=self.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=errors,
                )
                while True:
                    self.tick()
                    if time.monotonic() >= self.deadline:
                        raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")
                    if stderr.stat().st_size > 65536:
                        raise RepositoryError(422, "REPOSITORY_OUTPUT_LIMIT")
                    if stdout.stat().st_size > limit:
                        if allow_limit:
                            return None
                        raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
            self.tick()
            if time.monotonic() >= self.deadline:
                raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")
            if stderr.stat().st_size > 65536:
                raise RepositoryError(422, "REPOSITORY_OUTPUT_LIMIT")
            if stdout.stat().st_size > limit:
                if allow_limit:
                    return None
                raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
            if process.returncode not in codes:
                raise RepositoryError(422, "REPOSITORY_PARSE_FAILED")
            if exit_code:
                return process.returncode
            raw = stdout.read_bytes()
            if len(arguments) == 3 and arguments[:2] == ["cat-file", "commit"]:
                require(object_hash("commit", raw) == arguments[2], "EVAL_OBJECT_DRIFT")
            if len(arguments) == 3 and arguments[:2] == ["cat-file", "blob"]:
                require(object_hash("blob", raw) == arguments[2], "EVAL_OBJECT_DRIFT")
            return raw
        finally:
            if process is not None and process.poll() is None:
                if os.name == "nt":
                    terminate(process)
                else:
                    process.kill()
                    process.wait(timeout=10)
            stdout.unlink(missing_ok=True)
            stderr.unlink(missing_ok=True)


def _reader(source, work, check):
    source, work = assets.safe_path(Path(source)), assets.safe_path(Path(work))
    receipt = assets.read_json(source / "acquisition.json", check, maximum=65536)
    assets.verify_seal(receipt)
    require(receipt.get("repository") == REPOSITORY)
    bare = source / "source.git"
    environment = native_environment(work, bare)
    reader = Reader(bare, work, environment, check, time.monotonic() + 30)
    require(reader.command(["rev-parse", "--is-bare-repository"]).strip() == b"true")
    require(
        reader.command(["rev-parse", "--is-shallow-repository"]).strip() == b"false",
        "EVAL_SHALLOW_SOURCE",
    )
    return reader, receipt


def _metadata(reader, sha):
    reader.deadline = time.monotonic() + 30
    raw = reader.command(["cat-file", "commit", sha], limit=METADATA_LIMIT)
    clocks = commit_clocks(raw, sha)
    headers, separator, message = raw.partition(b"\n\n")
    require(bool(separator))
    try:
        encoding = next(
            (r[9:].decode("ascii") for r in headers.splitlines() if r.startswith(b"encoding ")),
            "utf-8",
        )
        decoded = message.decode(encoding, "strict")
        if "\0" in decoded:
            decoded = None
    except (UnicodeError, LookupError):
        decoded = None
    return clocks | {"message_hash": hashlib.sha256(message).hexdigest()}, decoded


def _excluded(dataset, check):
    dataset = assets.safe_path(Path(dataset))
    manifest = assets.read_json(dataset / "manifest.json", check, maximum=2 * 1024**2)
    assets.verify_seal(manifest)
    members = set()
    count = 0
    for part in PARTITIONS:
        path = dataset / (part + ".csv")
        require(assets.file_digest(path, check) == manifest["files"][part], "EVAL_EXCLUSION_DRIFT")
        with path.open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream)
            require(tuple(rows.fieldnames or ()) == CSV_FIELDS)
            for row in rows:
                count += 1
                # Windows parent-identity queries are relatively expensive. Keep
                # cancellation bounded without querying the OS for every CSV row.
                if count % 128 == 0:
                    check()
                require(count <= 200000, "EVAL_EXCLUSION_LIMIT")
                require(
                    isinstance(row["canonical_repository"], str)
                    and len(row["canonical_repository"]) <= 128
                )
                sha_valid(row["commit_id"].encode("ascii"))
                members.add((row["canonical_repository"], row["commit_id"]))
        check()
    return manifest, members


def declare(source, dataset, reservation, weights, output, expected_head, check=checkpoint):
    source, dataset, reservation, weights, output = [
        assets.safe_path(Path(p)) for p in (source, dataset, reservation, weights, output)
    ]
    require(
        all(
            not output.is_relative_to(p) and not p.is_relative_to(output)
            for p in (source, dataset, reservation, weights)
        ),
        "EVAL_OUTPUT_OVERLAP",
    )
    execution = _execution()
    inventory = _inventory(source, check)
    old, excluded = _excluded(dataset, check)
    reserved = artifacts.verify(reservation, check)
    records = assets.read_json(reservation / "reservation.json", check)["records"]
    ids = {r["commit_id"] for r in records}
    require(len(ids) == len(records) <= 64)
    weight = artifacts.verify(weights, check)
    require(weight.get("version") == "native-evaluation-weight-evidence-v1")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".declare-" + uuid4().hex)
    attempt.mkdir()
    with TemporaryDirectory(dir=attempt) as temporary:
        reader, receipt = _reader(source, Path(temporary), check)
        require(receipt["head"] == expected_head, "EVAL_HEAD_CONFLICT")
        raw = reader.command(
            ["rev-list", "--timestamp", "--parents", expected_head, "--"], limit=4 * 1024**2
        )
        reachable = {}
        for line in raw.splitlines():
            fields = line.split()
            sha = sha_valid(fields[1])
            require(sha not in reachable)
            reachable[sha] = {
                "commit_id": sha,
                "committer_epoch": int(fields[0]),
                "parents": [sha_valid(p) for p in fields[2:]],
            }
        observations, targets = [], []
        for sha, item in sorted(
            reachable.items(), key=lambda pair: (pair[1]["committer_epoch"], pair[0])
        ):
            if START <= item["committer_epoch"] < STOP:
                check()
                metadata, _ = _metadata(reader, sha)
                require(all(metadata[k] == item[k] for k in item), "EVAL_CLOCK_DRIFT")
                observations.append(metadata)
                if (
                    item["committer_epoch"] < TARGET_STOP
                    and len(item["parents"]) == 1
                    and (REPOSITORY, sha) not in excluded
                ):
                    targets.append(metadata | {"role": role(item["committer_epoch"])})
            require(
                len(observations) <= MAX_OBSERVATIONS and len(targets) <= MAX_TARGETS,
                "EVAL_POPULATION_LIMIT",
            )
        require(
            targets and ids <= {r["commit_id"] for r in targets if r["role"] == ROLES[3]},
            "EVAL_RESERVATION_ROLE",
        )
        acquired = datetime.fromisoformat(receipt["acquired_at"])
        require(acquired.utcoffset() is not None and int(acquired.timestamp()) >= STOP)
        head_metadata, _ = _metadata(reader, expected_head)
        require(
            STOP <= head_metadata["committer_epoch"] <= int(acquired.timestamp()),
            "EVAL_OBSERVATION_HEAD_RANGE",
        )
        require(
            _inventory(source, check) == inventory and _execution() == execution,
            "EVAL_DECLARATION_DRIFT",
        )
    value = {
        "version": VERSION,
        "source_receipt": receipt,
        "source_inventory": inventory,
        "execution": execution,
        "old_dataset": old,
        "old_exclusion_count": len(excluded),
        "old_exclusion_hash": content_hash(sorted(excluded)),
        "excluded_source_ids": sorted(
            sha for repo, sha in excluded if repo == REPOSITORY and sha in reachable
        ),
        "reservation_hash": reserved["content_hash"],
        "reserved_identities": sorted(ids),
        "weight_evidence_hash": weight["content_hash"],
        "targets": targets,
        "observations": observations,
        "reachable_metadata": reachable,
        "rev_list_sha256": hashlib.sha256(raw).hexdigest(),
        "horizon_seconds": HORIZON,
        "hypothetical_lag_seconds": LAG,
        "issue_observations": False,
        "label_kind": "provisional_szz_silver",
        "qualification_promoted": False,
    }
    _atomic(attempt / "declaration.json", assets.sealed(value))
    require(
        assets.read_json(dataset / "manifest.json", check, maximum=2 * 1024**2) == old
        and all(
            assets.file_digest(dataset / (p + ".csv"), check) == old["files"][p] for p in PARTITIONS
        ),
        "EVAL_EXCLUSION_DRIFT",
    )
    require(
        artifacts.verify(reservation, check) == reserved
        and artifacts.verify(weights, check) == weight,
        "EVAL_DEPENDENCY_DRIFT",
    )
    require(
        _inventory(source, check) == inventory and _execution() == execution,
        "EVAL_DECLARATION_DRIFT",
    )
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION + "-declaration",
            "assembled_at": datetime.now(UTC).isoformat(),
            "target_count": len(targets),
            "observation_count": len(observations),
            "sources": execution["sources"],
        },
        ["declaration.json"],
    )


def load_declaration(directory, check=checkpoint):
    directory = assets.safe_path(Path(directory))
    manifest = artifacts.verify(directory, check)
    require(manifest.get("version") == VERSION + "-declaration")
    value = assets.read_json(directory / "declaration.json", check, maximum=MAX_UNIT)
    assets.verify_seal(value)
    validate_declaration(value)
    require(manifest["sources"] == value["execution"]["sources"])
    return value


def validate_declaration(value):
    assets.verify_seal(value)
    require(
        value.get("version") == VERSION and value["execution"] == _execution(),
        "EVAL_EXECUTION_DRIFT",
    )
    require(
        0 < len(value["targets"]) <= MAX_TARGETS and len(value["observations"]) <= MAX_OBSERVATIONS
    )
    require(len({r["commit_id"] for r in value["targets"]}) == len(value["targets"]))
    selected = [
        r | {"role": role(r["committer_epoch"])}
        for r in value["observations"]
        if START <= r["committer_epoch"] < TARGET_STOP
        and len(r["parents"]) == 1
        and r["commit_id"] not in set(value["excluded_source_ids"])
    ]
    require(
        selected == value["targets"]
        and value["horizon_seconds"] == HORIZON
        and value["hypothetical_lag_seconds"] == LAG,
        "EVAL_ROLE_DRIFT",
    )
    observations = value["observations"]
    require(
        len({r["commit_id"] for r in observations}) == len(observations),
        "EVAL_OBSERVATIONS_INCOMPLETE",
    )
    expected = sorted(
        (r for r in value["reachable_metadata"].values() if START <= r["committer_epoch"] < STOP),
        key=lambda r: (r["committer_epoch"], r["commit_id"]),
    )
    require(
        len(expected) == len(observations)
        and all(
            all(actual[k] == v for k, v in item.items())
            for item, actual in zip(expected, observations, strict=True)
        ),
        "EVAL_OBSERVATION_DRIFT",
    )
    require(
        set(value["reserved_identities"])
        <= {r["commit_id"] for r in value["targets"] if r["role"] == ROLES[3]},
        "EVAL_RESERVATION_ROLE",
    )
    require(
        value["issue_observations"] is False
        and value["qualification_promoted"] is False
        and value["label_kind"] == "provisional_szz_silver"
    )


class Context:
    def __init__(self, declaration, source, cache, reader, check):
        self.declaration, self.source, self.cache, self.reader, self.check = (
            declaration,
            source,
            cache,
            reader,
            check,
        )
        self.targets = {r["commit_id"]: r for r in declaration["targets"]}
        self.units = {}
        self.metadata = {}
        self.reused = 0
        self.created = 0
        self.used_cached = set()
        self.capacity(force=True)
        expected_observations = {r["commit_id"]: r for r in declaration["observations"]}
        for path in cache.iterdir():
            assets.safe_path(path)
            require(path.is_file(), "EVAL_CACHE_UNKNOWN")
            if path.name in {"cache.json", ".publication-lock-v2"} or re.fullmatch(
                r"\.unit-part-[0-9a-f]{32}", path.name
            ):
                continue
            require(
                path.is_file()
                and re.fullmatch(
                    r"(?:target|observation)-[0-9a-f]{40}-[0-9a-f]{64}\.json", path.name
                ),
                "EVAL_CACHE_UNKNOWN",
            )
            kind, sha, digest = path.name[:-5].split("-")
            record = assets.file_digest(path, check, maximum=MAX_UNIT)
            require(record["sha256"] == digest, "EVAL_CACHE_DRIFT")
            unit = assets.read_json(path, check, maximum=MAX_UNIT)
            assets.verify_seal(unit)
            require(
                unit.get("binding") == declaration["content_hash"]
                and unit.get("kind") == kind
                and unit.get("sha") == sha,
                "EVAL_CACHE_BINDING",
            )
            require(
                sha in (self.targets if kind == "target" else expected_observations)
                and unit["value"]["metadata"] == expected_observations[sha],
                "EVAL_CACHE_BINDING",
            )
            require((kind, sha) not in self.units, "EVAL_CACHE_CONFLICT")
            if kind == "observation":
                verify_observation(unit["value"], declaration)
            self.units[kind, sha] = unit
        self.initial_units = set(self.units)
        self.capacity()

    def capacity(self, *, force=False):
        self.check()
        if not force and self.created - getattr(self, "capacity_scan_at", -64) < 64:
            return self.accounted_count, self.accounted_bytes
        count, total = 0, 0
        for path in self.cache.iterdir():
            count += 1
            require(count <= MAX_FILES, "EVAL_CACHE_LIMIT")
            require(assets.safe_path(path).is_file(), "EVAL_CACHE_UNKNOWN")
            require(
                path.name in {"cache.json", ".publication-lock-v2"}
                or re.fullmatch(r"\.unit-part-[0-9a-f]{32}", path.name)
                or re.fullmatch(
                    r"(?:target|observation)-[0-9a-f]{40}-[0-9a-f]{64}\.json", path.name
                ),
                "EVAL_CACHE_UNKNOWN",
            )
            total += path.stat().st_size
            require(total <= MAX_TREE, "EVAL_CACHE_LIMIT")
            if count % 128 == 0:
                self.check()
        self.accounted_count, self.accounted_bytes = count, total
        self.capacity_scan_at = self.created
        return count, total

    def save(self, kind, sha, value):
        count, total = self.capacity()
        require(_execution() == self.declaration["execution"], "EVAL_EXECUTION_DRIFT")
        unit = assets.sealed(
            {"kind": kind, "sha": sha, "binding": self.declaration["content_hash"], "value": value}
        )
        raw = canonical(unit) + b"\n"
        require(len(raw) <= MAX_UNIT, "EVAL_UNIT_LIMIT")
        require(count + 2 <= MAX_FILES and total + 2 * len(raw) <= MAX_TREE, "EVAL_CACHE_LIMIT")
        path = self.cache / f"{kind}-{sha}-{hashlib.sha256(raw).hexdigest()}.json"
        _atomic(path, unit)
        self.units[kind, sha] = unit
        self.created += 1
        self.accounted_count += 1
        self.accounted_bytes += len(raw)
        self.capacity()
        return value

    def raw(self, sha):
        if sha not in self.metadata:
            require(sha in self.declaration["reachable_metadata"], "EVAL_FOREIGN_BLAME")
            value, message = _metadata(self.reader, sha)
            expected = self.declaration["reachable_metadata"][sha]
            require(all(value[k] == v for k, v in expected.items()), "EVAL_CLOCK_DRIFT")
            self.metadata[sha] = value, message
        return self.metadata[sha]

    def target(self, sha):
        require(sha in self.targets)
        if ("target", sha) in self.units:
            self.reused += 1
            self.used_cached.add(("target", sha))
            return self.units["target", sha]["value"]
        metadata, _ = self.raw(sha)
        self.reader.deadline = time.monotonic() + 30
        try:
            record = self.reader.record(sha)
            require(
                record["commit"]["parents"] == metadata["parents"]
                and epoch(record["commit"]["committer_time"]) == metadata["committer_epoch"],
                "EVAL_RECORD_DRIFT",
            )
            value = {
                "metadata": metadata,
                "parse_status": record["commit"]["parse_status"],
                "files": [
                    {k: v for k, v in f.items() if k not in {"diff_text", "line_numbers"}}
                    for f in record["files"]
                ],
            }
        except RepositoryError as exc:
            value = {
                "metadata": metadata,
                "parse_status": "unavailable",
                "reason": exc.code,
                "files": [],
            }
        return self.save("target", sha, value)

    def observation(self, expected):
        sha = expected["commit_id"]
        if ("observation", sha) in self.units:
            self.reused += 1
            self.used_cached.add(("observation", sha))
            return self.units["observation", sha]["value"]
        metadata, message = self.raw(sha)
        require(metadata == expected, "EVAL_OBSERVATION_DRIFT")
        if message is None:
            return self.save(
                "observation",
                sha,
                {
                    "metadata": metadata,
                    "disposition": "unknown",
                    "trace": {
                        "status": "unknown",
                        "reasons": ["message_encoding"],
                        "files": [],
                        "links": [],
                    },
                    "global_gaps": ["message_encoding"],
                    "target_gaps": [],
                    "positive_targets": [],
                    "outside_frame": [],
                    "target_evidence": {},
                },
            )
        commit = {
            "sha": sha,
            "parents": metadata["parents"],
            "parse_status": "parsed",
            "committer_time": datetime.fromtimestamp(metadata["committer_epoch"], UTC).replace(
                tzinfo=None
            ),
        }
        files = []
        native_failure = None
        if re.search(fix_rules.KEYWORD, message, re.I):
            try:
                self.reader.deadline = time.monotonic() + 30
                record = self.reader.record(sha)
                commit, files = record["commit"], record["files"]
                require(
                    epoch(commit["committer_time"]) == metadata["committer_epoch"]
                    and commit["parents"] == metadata["parents"],
                    "EVAL_RECORD_DRIFT",
                )
            except RepositoryError as exc:
                native_failure = exc.code
        assessment = fix_rules.evaluate(
            message, commit["parse_status"], files, [], include_medium=True
        )
        if native_failure:
            trace = {"status": "unknown", "reasons": [native_failure], "files": [], "links": []}
        else:
            context = self

            class Eligible(dict):
                def __contains__(self, identity):
                    return (
                        identity in context.targets
                        and context.target(identity)["parse_status"] == "parsed"
                    )

                def __getitem__(self, identity):
                    return datetime.fromtimestamp(
                        context.targets[identity]["committer_epoch"], UTC
                    ).replace(tzinfo=None)

            self.reader.deadline = time.monotonic() + 30
            source = {
                "visible": True,
                "candidate": assessment["rule_candidate"],
                "available_at": datetime.fromtimestamp(
                    metadata["committer_epoch"] + LAG, UTC
                ).isoformat(),
            }
            try:
                trace = szz_engine.trace(self.reader, source, commit, files, Eligible())
            except RepositoryError as exc:
                trace = {"status": "unknown", "reasons": [exc.code], "files": [], "links": []}
        global_gaps = []
        target_gaps = set()
        positive = set()
        outside = []
        if (
            native_failure
            or any(f["status"] == "unknown" for f in trace["files"])
            or trace["status"] == "unknown"
            and not trace["files"]
        ):
            global_gaps = trace["reasons"] or ["unmapped_observation"]
        for link in trace["links"]:
            identity = link["blamed_sha"]
            if identity in self.targets:
                if link["eligible"]:
                    positive.add(identity)
                else:
                    target_gaps.add(identity)
            else:
                self.raw(identity)
                self.reader.deadline = time.monotonic() + 30
                if self.reader.ancestor(identity, link["parent_sha"]):
                    outside.append(identity)
                else:
                    global_gaps.append("outside_identity_unverified")
        return self.save(
            "observation",
            sha,
            {
                "metadata": metadata,
                "disposition": assessment["disposition"],
                "rule_hash": fix_rules.RULE_HASH,
                "trace": trace,
                "global_gaps": sorted(set(global_gaps)),
                "target_gaps": sorted(target_gaps),
                "positive_targets": sorted(positive),
                "outside_frame": sorted(set(outside)),
                "target_evidence": {
                    identity: self.target(identity) for identity in sorted(positive | target_gaps)
                },
            },
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


def observe(declaration, source, cache, output, *, pilot=None, check=checkpoint):
    value = load_declaration(declaration, check)
    source, cache, output = [assets.safe_path(Path(p)) for p in (source, cache, output)]
    declaration = assets.safe_path(Path(declaration))
    require(
        all(
            not a.is_relative_to(b) and not b.is_relative_to(a)
            for a, b in [
                (cache, source),
                (output, source),
                (cache, declaration),
                (output, declaration),
                (cache, output),
            ]
        ),
        "EVAL_OUTPUT_OVERLAP",
    )
    require(_inventory(source, check) == value["source_inventory"], "EVAL_SOURCE_DRIFT")
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".observe-" + uuid4().hex)
    attempt.mkdir()
    with publication(cache):
        marker = assets.sealed({"version": VERSION, "declaration": value["content_hash"]})
        if (cache / "cache.json").exists():
            require(assets.read_json(cache / "cache.json", check) == marker, "EVAL_CACHE_BINDING")
        else:
            _atomic(cache / "cache.json", marker)
        with TemporaryDirectory(dir=attempt) as temporary:
            reader, receipt = _reader(source, Path(temporary), check)
            require(receipt == value["source_receipt"], "EVAL_SOURCE_DRIFT")
            context = Context(value, source, cache, reader, check)
            planned = value["observations"]
            if pilot is not None:
                require(type(pilot) is int and 1 <= pilot <= 16, "EVAL_PILOT_LIMIT")
                planned = sorted(planned, key=lambda r: r["commit_id"])[:pilot]
            observations = []
            with (attempt / "observations.jsonl").open("xb") as stream:
                for i, item in enumerate(planned):
                    check()
                    unit = context.observation(item)
                    observations.append(unit)
                    raw = canonical(unit) + b"\n"
                    require(
                        stream.tell() + len(raw) <= MAX_TREE - 3 * MAX_UNIT, "EVAL_OUTPUT_LIMIT"
                    )
                    stream.write(raw)
                    if i % 10 == 0:
                        print(f"evaluation observations {i + 1}/{len(planned)}", flush=True)
            context.capacity(force=True)
            require(
                _inventory(source, check) == value["source_inventory"]
                and _execution() == value["execution"],
                "EVAL_FINAL_DRIFT",
            )
            target_rows = outcomes(value, observations) if pilot is None else []
            support = {
                r: dict(
                    Counter(
                        "unknown" if t["provisional_label"] is None else str(t["provisional_label"])
                        for t in target_rows
                        if t["role"] == r
                    )
                )
                for r in ROLES
            }
            summary = {
                "version": VERSION,
                "complete_population": pilot is None,
                "planned_observations": len(value["observations"]),
                "completed_observations": len(observations),
                "targets": len(value["targets"]),
                "support": support,
                "calibration_positive_min10": support[ROLES[1]].get("1", 0) >= 10,
                "threshold_positive_min10": support[ROLES[2]].get("1", 0) >= 10,
                "global_gap_observations": sum(bool(r["global_gaps"]) for r in observations),
                "units_created": context.created,
                "prior_completed_units_reused": len(context.used_cached & context.initial_units),
                "cache_hits_total": context.reused,
                "label_kind": "provisional_szz_silver",
                "human_review_complete": False,
                "native14_features_verified": False,
                "new_representation_verified": False,
                "contamination_certified": False,
                "research_training_ready": False,
                "historical_replay": False,
                "candidate_release": False,
            }
            _atomic(attempt / "outcomes.json", {"records": target_rows})
            _atomic(attempt / "summary.json", summary)
            _atomic(attempt / "declaration.json", value)
    require(
        _inventory(source, check) == value["source_inventory"]
        and _execution() == value["execution"],
        "EVAL_FINAL_DRIFT",
    )
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION + ("-pilot" if pilot is not None else "-audit"),
            "declaration_hash": value["content_hash"],
            "assembled_at": datetime.now(UTC).isoformat(),
            "complete_population": pilot is None,
            "target_count": len(value["targets"]),
            "sources": value["execution"]["sources"],
        },
        ["observations.jsonl", "outcomes.json", "summary.json", "declaration.json"],
    )


def verify_audit(directory, check=checkpoint):
    directory = assets.safe_path(Path(directory))
    manifest = artifacts.verify(directory, check)
    require(
        manifest.get("version") in {VERSION + "-audit", VERSION + "-pilot"}, "EVAL_AUDIT_VERSION"
    )
    require(
        set(manifest["files"])
        == {"observations.jsonl", "outcomes.json", "summary.json", "declaration.json"}
    )
    require(sum(r["bytes"] for r in manifest["files"].values()) <= MAX_TREE, "EVAL_OUTPUT_LIMIT")
    declaration = assets.read_json(directory / "declaration.json", check, maximum=MAX_UNIT)
    assets.verify_seal(declaration)
    validate_declaration(declaration)
    require(
        declaration["execution"] == _execution()
        and declaration["execution"]["sources"] == manifest["sources"],
        "EVAL_EXECUTION_DRIFT",
    )
    require(declaration["content_hash"] == manifest["declaration_hash"])
    summary = assets.read_json(directory / "summary.json", check, maximum=MAX_UNIT)
    rows = []
    with (directory / "observations.jsonl").open("rb") as stream:
        while raw := stream.readline(MAX_UNIT + 1):
            check()
            require(len(raw) <= MAX_UNIT and len(rows) < MAX_OBSERVATIONS, "EVAL_UNIT_LIMIT")
            import json

            rows.append(json.loads(raw))
    require(summary["completed_observations"] == len(rows))
    expected_observations = {r["commit_id"]: r for r in declaration["observations"]}
    for row in rows:
        require(
            row["metadata"] == expected_observations[row["metadata"]["commit_id"]],
            "EVAL_OBSERVATION_DRIFT",
        )
        verify_observation(row, declaration)
    actual = assets.read_json(directory / "outcomes.json", check, maximum=MAX_UNIT)["records"]
    complete = manifest["version"] == VERSION + "-audit"
    require(summary["complete_population"] == complete)
    if complete:
        expected = outcomes(declaration, rows)
        require(actual == expected, "EVAL_OUTCOME_DRIFT")
        support = {
            r: dict(
                Counter(
                    "unknown" if t["provisional_label"] is None else str(t["provisional_label"])
                    for t in expected
                    if t["role"] == r
                )
            )
            for r in ROLES
        }
        require(
            summary["support"] == support and summary["targets"] == len(expected),
            "EVAL_SUPPORT_DRIFT",
        )
        require(
            summary["calibration_positive_min10"] == (support[ROLES[1]].get("1", 0) >= 10)
            and summary["threshold_positive_min10"] == (support[ROLES[2]].get("1", 0) >= 10)
        )
    else:
        require(not actual, "EVAL_PILOT_NOT_POPULATION")
    require(
        all(
            summary[k] is False
            for k in [
                "human_review_complete",
                "native14_features_verified",
                "new_representation_verified",
                "contamination_certified",
                "research_training_ready",
                "historical_replay",
                "candidate_release",
            ]
        ),
        "EVAL_QUALIFICATION_DRIFT",
    )
    return manifest, summary


def summarize(directory, output, check=checkpoint):
    manifest, summary = verify_audit(directory, check)
    output = assets.safe_path(Path(output))
    require(
        not output.is_relative_to(Path(directory)) and not Path(directory).is_relative_to(output),
        "EVAL_OUTPUT_OVERLAP",
    )
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".verify-" + uuid4().hex)
    attempt.mkdir()
    _atomic(
        attempt / "verification.json",
        {
            "audit_manifest_hash": manifest["content_hash"],
            "summary": summary,
            "scope": (
                "Re-derived horizon outcomes from preserved line evidence; "
                "source Git replay is a separate check"
            ),
            "qualified_for_training_or_release": False,
        },
    )
    require(artifacts.verify(Path(directory), check) == manifest, "EVAL_AUDIT_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION + "-verification",
            "assembled_at": datetime.now(UTC).isoformat(),
            "sources": artifacts.sources(SOURCES),
        },
        ["verification.json"],
    )
