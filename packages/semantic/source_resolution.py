"""Separate immutable parent-edge source policy; v1 artifacts remain untouched."""

import copy
import hashlib
import json
import os
import re
import stat
import time
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import fix_rules, szz_engine
from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical
from packages.repositories.parse_policy import METADATA_LIMIT
from packages.repositories.safety import RepositoryError
from packages.semantic import evaluation_population as original
from packages.semantic import native_runtime
from packages.semantic.admission_git import object_hash

VERSION = "native-parent-consensus-source-v3"
SOURCES = (
    *original.SOURCES,
    "packages/semantic/native_runtime.py",
    "packages/semantic/source_resolution.py",
    "packages/semantic/source_benchmark.py",
    "scripts/semantic_source_resolution.py",
)
BLOB_CACHE_BYTES = 8 * 1024**2
BLOB_CACHE_ENTRIES = 512
ANCESTRY_CACHE_ENTRIES = 4096
COMMANDS = {"rev-parse", "rev-list", "cat-file", "diff-tree", "ls-tree", "blame", "merge-base"}
OUTPUT_MAX_ENTRIES = 4096
OUTPUT_MAX_BYTES = 1024**3
PUBLICATION_DELAYS = (0.1, 0.2, 0.4, 0.8)


def output_budget(root, check, *, extra_entries=0, extra_bytes=0):
    """Count the whole owned result namespace, including every failed attempt."""
    root = assets.safe_path(Path(root))
    count = total = 0
    pending = [(root, 0)]
    try:
        while pending:
            directory, depth = pending.pop()
            original.require(depth <= 8, "SOURCE_OUTPUT_LIMIT")
            with os.scandir(directory) as entries:
                for entry in entries:
                    check()
                    path = assets.safe_path(Path(entry.path))
                    value = path.lstat()
                    count += 1
                    if stat.S_ISDIR(value.st_mode):
                        pending.append((path, depth + 1))
                    else:
                        original.require(
                            stat.S_ISREG(value.st_mode) and value.st_nlink == 1,
                            "SOURCE_OUTPUT_SPECIAL_FILE",
                        )
                        total += value.st_size
                    original.require(
                        count + extra_entries <= OUTPUT_MAX_ENTRIES
                        and total + extra_bytes <= OUTPUT_MAX_BYTES,
                        "SOURCE_OUTPUT_LIMIT",
                    )
    except OSError as exc:
        raise original.SemanticError("SOURCE_OUTPUT_UNREADABLE") from exc
    original.require(
        count + extra_entries <= OUTPUT_MAX_ENTRIES and total + extra_bytes <= OUTPUT_MAX_BYTES,
        "SOURCE_OUTPUT_LIMIT",
    )
    return count, total


class OutputStore:
    """Private attempt under an exclusive output lock, with pre-write peak checks."""

    def __init__(self, root, attempt, check, reserve):
        self.root, self.attempt, self.check, self.reserve = root, attempt, check, reserve
        self.publication_retries = 0

    def ensure(self, *, entries=0, size=0):
        return output_budget(
            self.root,
            self.check,
            extra_entries=entries + (4 if self.reserve else 0),
            extra_bytes=size + self.reserve,
        )

    def path(self, name):
        original.require(
            isinstance(name, str)
            and Path(name).name == name
            and name not in {".", "..", "manifest.json"}
            and "\\" not in name,
            "SOURCE_OUTPUT_NAME",
        )
        return assets.safe_path(self.attempt / name)

    def json(self, name, value):
        path = self.path(name)
        original.require(not path.exists(), "SOURCE_OUTPUT_EXISTS")
        raw = canonical(value) + b"\n"
        original.require(len(raw) <= original.MAX_UNIT, "SOURCE_UNIT_LIMIT")
        self.ensure(entries=2, size=2 * len(raw))
        original._atomic(path, value)

    @contextmanager
    def lines(self, name):
        self.ensure(entries=1)
        with self.path(name).open("xb") as stream:
            yield stream

    def append(self, stream, raw):
        original.require(
            Path(stream.name).parent == self.attempt and len(raw) <= original.MAX_UNIT,
            "SOURCE_UNIT_LIMIT",
        )
        stream.flush()
        self.ensure(size=len(raw))
        stream.write(raw)

    def copy(self, name, source):
        source = assets.safe_path(Path(source))
        with source.open("rb") as incoming, self.lines(name) as outgoing:
            while chunk := incoming.read(1024**2):
                self.append(outgoing, chunk)

    def publish(self, metadata, names):
        original.require(all(self.path(name).is_file() for name in names), "SOURCE_OUTPUT_MISSING")
        files = {
            name: original.artifacts.file_digest(self.path(name), self.check) for name in names
        }
        manifest = assets.sealed(metadata | {"files": files})
        self.ensure(entries=2, size=2 * (len(canonical(manifest)) + 1))
        original._atomic(self.attempt / "manifest.json", manifest)
        target = assets.safe_path(self.root / original.content_hash(manifest))
        for index in range(len(PUBLICATION_DELAYS) + 1):
            self.check()
            if target.exists():
                original.require(
                    original.artifacts.verify(target, self.check) == manifest,
                    "SOURCE_PUBLICATION_CONFLICT",
                )
                self.ensure()
                return target
            try:
                self.attempt.rename(target)
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {32, 33}:
                    raise original.SemanticError("SOURCE_PUBLICATION_FAILED") from exc
                if index == len(PUBLICATION_DELAYS):
                    raise original.SemanticError("SOURCE_PUBLICATION_BUSY") from exc
                self.publication_retries += 1
                time.sleep(PUBLICATION_DELAYS[index])
                original.require(
                    assets.read_json(
                        self.attempt / "manifest.json", self.check, maximum=original.MAX_UNIT
                    )
                    == manifest
                    and all(
                        original.artifacts.file_digest(self.path(name), self.check) == value
                        for name, value in files.items()
                    ),
                    "SOURCE_PUBLICATION_DRIFT",
                )
            else:
                self.ensure()
                return target
        raise original.SemanticError("SOURCE_PUBLICATION_BUSY")


@contextmanager
def output_store(root, prefix, check):
    original.require(
        prefix in {"declare", "resolve", "verify", "benchmark", "safety"}, "SOURCE_OUTPUT_NAME"
    )
    root = assets.safe_path(Path(root))
    root.mkdir(parents=True, exist_ok=True)
    # Reserve lock metadata and the first atomic unit before creating an attempt.
    output_budget(root, check, extra_entries=4, extra_bytes=4096)
    with original.publication(root):
        reserve = 2 * original.MAX_UNIT if prefix in {"resolve", "benchmark", "safety"} else 0
        output_budget(root, check, extra_entries=3, extra_bytes=reserve)
        attempt = root / ("." + prefix + "-v2-" + uuid4().hex)
        attempt.mkdir()
        yield OutputStore(root, attempt, check, reserve)


class Reader(original.Reader):
    """New bound runtime; caches are optional and local to this source execution."""

    def __init__(self, *args, source_id, cache=False, **kwargs):
        original.require(isinstance(source_id, str) and re.fullmatch("[0-9a-f]{64}", source_id))
        original.require(type(cache) is bool)
        super().__init__(*args, **kwargs)
        self.source_id = source_id
        self.cached = cache
        self._binding = self.binding()
        self.blobs = OrderedDict()
        self.blob_bytes = 0
        self.ancestry = OrderedDict()
        self.statistics = Counter()

    def binding(self):
        return (
            VERSION,
            self.source_id,
            self.cached,
            str(self.bare),
            self.git,
            tuple(sorted(self.environment.items())),
        )

    def checked(self):
        original.require(self.binding() == self._binding, "SOURCE_RUNTIME_BINDING_CHANGED")
        self.tick()
        if time.monotonic() >= self.deadline:
            raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")

    def command(
        self, arguments, limit=METADATA_LIMIT, allow_limit=False, codes=(0,), exit_code=False
    ):
        self.checked()
        original.require(arguments and arguments[0] in COMMANDS, "SOURCE_GIT_COMMAND")
        original.require(type(limit) is int and 0 < limit <= 8 * 1024**2, "SOURCE_GIT_LIMIT")
        self.statistics["command_attempts"] += 1
        self.statistics["command_" + arguments[0]] += 1
        try:
            result = native_runtime.run(
                [self.git, "--literal-pathspecs", "-C", str(self.bare), *arguments],
                directory=self.attempt,
                environment=self.environment,
                check=self.checked,
                deadline=self.deadline,
                limit=limit,
                codes=codes,
                allow_limit=allow_limit,
            )
        except RepositoryError as exc:
            # Legacy parsing intentionally turns RepositoryError into an unavailable
            # unit. Ownership/cleanup failures must instead abort the entire phase.
            if exc.code.startswith("NATIVE_"):
                raise original.SemanticError(exc.code) from exc
            raise
        self.statistics["cleanup_confirmed"] += int(result.cleanup_complete)
        raw = result.data
        if raw is None:
            return None
        if len(arguments) == 3 and arguments[:2] in (
            ["cat-file", "commit"],
            ["cat-file", "blob"],
        ):
            original.require(object_hash(arguments[1], raw) == arguments[2], "SOURCE_OBJECT_DRIFT")
        return result.returncode if exit_code else raw

    def blob(self, sha, mode):
        self.checked()
        key = sha, mode
        cacheable = self.cached and mode in {"100644", "100755"} and set(sha) != {"0"}
        if cacheable and key in self.blobs:
            self.blobs.move_to_end(key)
            self.statistics["blob_hits"] += 1
            return self.blobs[key]
        value = super().blob(sha, mode)
        if cacheable and value is not None and len(value) <= BLOB_CACHE_BYTES:
            while self.blobs and (
                len(self.blobs) >= BLOB_CACHE_ENTRIES
                or self.blob_bytes + len(value) > BLOB_CACHE_BYTES
            ):
                _, old = self.blobs.popitem(last=False)
                self.blob_bytes -= len(old)
            self.blobs[key] = value
            self.blob_bytes += len(value)
        return value

    def ancestor(self, base, head):
        self.checked()
        key = base, head
        if self.cached and key in self.ancestry:
            self.ancestry.move_to_end(key)
            self.statistics["ancestor_hits"] += 1
            return self.ancestry[key]
        value = super().ancestor(base, head)
        if self.cached:
            while len(self.ancestry) >= ANCESTRY_CACHE_ENTRIES:
                self.ancestry.popitem(last=False)
            self.ancestry[key] = value
        return value


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


def execution():
    """Same installed Git/Python baseline, with the separate executing source closure."""
    value = original._execution()
    return value | {"sources": original.artifacts.sources(SOURCES)}


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


def _file_evidence(files):
    return [
        copy.deepcopy(file)
        | {"diff_hash": hashlib.sha256((file.get("diff_text") or "").encode()).hexdigest()}
        for file in files
    ]


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


class Context(original.Context):
    """Immutable v2 units, separate binding/cache, original target parser and clocks."""

    def __init__(
        self,
        declaration,
        source,
        cache,
        reader,
        check,
        *,
        binding,
        execution,
        execution_check=None,
    ):
        original.require(re.fullmatch("[0-9a-f]{64}", binding), "SOURCE_CACHE_BINDING")
        self.declaration, self.source, self.cache, self.reader, self.check = (
            declaration,
            assets.safe_path(Path(source)),
            assets.safe_path(Path(cache)),
            reader,
            check,
        )
        self.binding = binding
        self.execution = execution
        self.execution_check = execution_check or globals()["execution"]
        self.targets = {row["commit_id"]: row for row in declaration["targets"]}
        self.expected = {row["commit_id"]: row for row in declaration["observations"]}
        self.units, self.metadata, self.native_commits = {}, {}, {}
        self.reused = self.created = 0
        self.used_cached = set()
        self.capacity(force=True)
        marker = assets.sealed({"version": VERSION, "binding": binding, "execution": execution})
        marker_path = self.cache / "cache.json"
        if marker_path.exists():
            original.require(
                assets.read_json(marker_path, check, maximum=original.MAX_UNIT) == marker,
                "SOURCE_CACHE_BINDING",
            )
        else:
            original._atomic(marker_path, marker)
        for path in sorted(self.cache.iterdir()):
            check()
            if not re.fullmatch(
                r"(?:target|observation)-[0-9a-f]{40}-[0-9a-f]{64}\.json", path.name
            ):
                continue  # capacity() has already rejected unknown paths.
            kind, sha, digest = path.name[:-5].split("-")
            original.require(
                assets.file_digest(path, check, maximum=original.MAX_UNIT)["sha256"] == digest,
                "SOURCE_CACHE_DRIFT",
            )
            unit = assets.read_json(path, check, maximum=original.MAX_UNIT)
            assets.verify_seal(unit)
            original.require(
                unit.get("binding") == binding
                and unit.get("kind") == kind
                and unit.get("sha") == sha
                and (kind, sha) not in self.units,
                "SOURCE_CACHE_BINDING",
            )
            value = unit["value"]
            if kind == "observation":
                verify_unit(value, declaration)
                original.require(
                    value["resolution"]["metadata"] == self.expected.get(sha),
                    "SOURCE_CACHE_BINDING",
                )
            else:
                verify_target(value, declaration)
                original.require(value["metadata"]["commit_id"] == sha, "SOURCE_CACHE_BINDING")
            self.units[kind, sha] = unit
        # Every embedded target parse must agree with its own immutable saved unit.
        for (kind, _), unit in self.units.items():
            if kind == "observation":
                for sha, target in unit["value"]["resolution"]["target_evidence"].items():
                    original.require(
                        self.units.get(("target", sha), {}).get("value") == target,
                        "SOURCE_TARGET_EVIDENCE_DRIFT",
                    )
        self.initial_units = set(self.units)
        self.capacity(force=True)

    def raw(self, sha):
        if sha not in self.metadata:
            original.require(sha in self.declaration["reachable_metadata"], "EVAL_FOREIGN_BLAME")
            self.reader.deadline = time.monotonic() + 30
            raw = self.reader.command(["cat-file", "commit", sha])
            value, message = decode_commit(raw, sha)
            expected = self.declaration["reachable_metadata"][sha]
            original.require(
                all(value[key] == item for key, item in expected.items()), "EVAL_CLOCK_DRIFT"
            )
            self.metadata[sha] = value, message
            self.native_commits[sha] = raw
        self.check()
        return self.metadata[sha]

    def save(self, kind, sha, value):
        self.check()
        original.require(self.execution_check() == self.execution, "SOURCE_EXECUTION_DRIFT")
        original.require((kind, sha) not in self.units, "SOURCE_CACHE_CONFLICT")
        if kind == "observation":
            verify_unit(value, self.declaration)
        else:
            original.require(kind == "target", "SOURCE_CACHE_BINDING")
            verify_target(value, self.declaration)
        count, total = self.capacity()
        unit = assets.sealed({"kind": kind, "sha": sha, "binding": self.binding, "value": value})
        raw = canonical(unit) + b"\n"
        original.require(len(raw) <= original.MAX_UNIT, "EVAL_UNIT_LIMIT")
        original.require(
            count + 2 <= original.MAX_FILES and total + 2 * len(raw) <= original.MAX_TREE,
            "EVAL_CACHE_LIMIT",
        )
        path = self.cache / f"{kind}-{sha}-{hashlib.sha256(raw).hexdigest()}.json"
        original._atomic(path, unit)
        self.units[kind, sha] = unit
        self.created += 1
        self.accounted_count += 1
        self.accounted_bytes += len(raw)
        return copy.deepcopy(value)

    def target(self, sha):
        # The frozen parser verifies actual target eligibility; fatal runtime errors
        # escape its RepositoryError handler through Reader.command above.
        self.check()
        if ("target", sha) in self.units:
            return copy.deepcopy(super().target(sha))
        parent_deadline = self.reader.deadline
        started = time.monotonic()
        try:
            return copy.deepcopy(super().target(sha))
        finally:
            # Metadata/target parsing retain their own bounded reads. Neither a
            # child's expired deadline nor its fresh budget replaces the parent's
            # remaining active time; whole-phase supervision continues throughout.
            self.reader.deadline = parent_deadline + (time.monotonic() - started)

    def _edge(self, metadata, parent, files, failure, assessment):
        context = self

        class Eligible(dict):
            def __contains__(self, sha):
                return sha in context.targets and context.target(sha)["parse_status"] == "parsed"

            def __getitem__(self, sha):
                return datetime.fromtimestamp(context.targets[sha]["committer_epoch"], UTC).replace(
                    tzinfo=None
                )

        if failure:
            trace = {"status": "unknown", "reasons": [failure], "files": [], "links": []}
        else:
            self.reader.deadline = time.monotonic() + 30
            try:
                trace = szz_engine.trace(
                    self.reader,
                    {
                        "visible": True,
                        "candidate": assessment["rule_candidate"],
                        "available_at": datetime.fromtimestamp(
                            metadata["committer_epoch"] + original.LAG, UTC
                        ).isoformat(),
                    },
                    {
                        "sha": metadata["commit_id"],
                        "parents": [parent],
                        "parse_status": "parsed",
                        "committer_time": datetime.fromtimestamp(
                            metadata["committer_epoch"], UTC
                        ).replace(tzinfo=None),
                    },
                    files,
                    Eligible(),
                )
            except RepositoryError as exc:
                trace = {"status": "unknown", "reasons": [exc.code], "files": [], "links": []}
        gaps = (
            list(trace["reasons"] or ["unmapped_observation"])
            if any(file["status"] == "unknown" for file in trace["files"])
            or trace["status"] == "unknown"
            and not trace["files"]
            else []
        )
        positives, target_gaps, outside = set(), set(), set()
        for link in trace["links"]:
            sha = link["blamed_sha"]
            if sha in self.targets:
                (positives if link["eligible"] else target_gaps).add(sha)
            else:
                self.raw(sha)
                self.reader.deadline = time.monotonic() + 30
                try:
                    verified = self.reader.ancestor(sha, parent)
                except RepositoryError:
                    verified = False
                if verified:
                    outside.add(sha)
                else:
                    gaps.append("outside_identity_unverified")
        return {
            "parent_sha": parent,
            "read_failure": failure,
            "files": _file_evidence(files),
            "observation": {
                "metadata": metadata | {"parents": [parent]},
                "trace": trace,
                "positive_targets": sorted(positives),
                "target_gaps": sorted(target_gaps),
                "global_gaps": sorted(set(gaps)),
                "outside_frame": sorted(outside),
                "target_evidence": {
                    sha: self.target(sha) for sha in sorted(positives | target_gaps)
                },
            },
        }

    def observation(self, expected):
        self.check()
        sha = expected["commit_id"]
        original.require(expected == self.expected.get(sha), "SOURCE_OBSERVATION_DRIFT")
        if ("observation", sha) in self.units:
            self.reused += 1
            self.used_cached.add(("observation", sha))
            return copy.deepcopy(self.units["observation", sha]["value"])
        metadata, message = self.raw(sha)
        original.require(metadata == expected, "SOURCE_OBSERVATION_DRIFT")
        pending = []
        if 0 < len(metadata["parents"]) <= 8:
            for parent in metadata["parents"]:
                files, failure = [], "message_encoding" if message is None else None
                if not failure:
                    self.reader.deadline = time.monotonic() + 30
                    try:
                        files = self.reader.files(sha, [parent])
                    except RepositoryError as exc:
                        failure = exc.code
                pending.append({"parent_sha": parent, "files": files, "read_failure": failure})
        assessment = _assessment(message, pending)
        edges = [
            self._edge(
                metadata, item["parent_sha"], item["files"], item["read_failure"], assessment
            )
            for item in pending
        ]
        return self.save(
            "observation",
            sha,
            {
                "version": VERSION,
                "raw_commit_hex": self.native_commits[sha].hex(),
                "assessment": assessment,
                "resolution": combine_edges(metadata, edges, self.declaration),
            },
        )


def _rows(path, check):
    path = assets.safe_path(Path(path))
    original.require(path.stat().st_size <= original.MAX_TREE, "SOURCE_OUTPUT_LIMIT")
    rows = []
    with path.open("rb") as stream:
        while raw := stream.readline(original.MAX_UNIT + 1):
            check()
            original.require(
                len(raw) <= original.MAX_UNIT and len(rows) < original.MAX_OBSERVATIONS,
                "SOURCE_UNIT_LIMIT",
            )
            value = json.loads(raw)
            original.require(raw == canonical(value) + b"\n", "SOURCE_NONCANONICAL_UNIT")
            rows.append(value)
    return rows


def _separate(*paths):
    paths = [assets.safe_path(Path(path)) for path in paths]
    original.require(
        all(
            not left.is_relative_to(right) and not right.is_relative_to(left)
            for i, left in enumerate(paths)
            for right in paths[i + 1 :]
        ),
        "SOURCE_OUTPUT_OVERLAP",
    )
    return paths


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


def declare(source, audit, plan, output, check=native_runtime.checkpoint):
    source, audit, plan, output = _separate(source, audit, plan, output)
    old_manifest, old_summary = original.verify_audit(audit, check)
    original.require(old_summary["complete_population"] is True, "SOURCE_OLD_AUDIT_INCOMPLETE")
    old = assets.read_json(audit / "declaration.json", check, maximum=original.MAX_UNIT)
    rows = _rows(audit / "observations.jsonl", check)
    inputs = assets.read_json(plan, check, maximum=original.MAX_UNIT)
    original.require(inputs["original_source_manifest"] == old_manifest["content_hash"])
    payloads = {
        name: old_manifest["files"][name]
        for name in ("declaration.json", "observations.jsonl", "outcomes.json")
    }
    original.require(payloads == inputs["original_scientific_payloads"], "SOURCE_OLD_PAYLOAD_DRIFT")
    _plan_matches(inputs, old, rows)
    original.require(original._inventory(source, check) == old["source_inventory"], "SOURCE_DRIFT")
    running = execution()
    value = assets.sealed(
        {
            "version": VERSION,
            "original": old,
            "original_manifest": old_manifest,
            "input_plan": inputs,
            "execution": running,
            "cache_default": False,
            "qualification_promoted": False,
        }
    )
    with output_store(output, "declare", check) as store:
        store.json("resolution.json", value)
        original.require(execution() == running, "SOURCE_EXECUTION_DRIFT")
        original.require(
            original._inventory(source, check) == old["source_inventory"], "SOURCE_DRIFT"
        )
        return store.publish(
            {"version": VERSION + "-declaration", "sources": running["sources"]},
            ["resolution.json"],
        )


def load_declaration(directory, check=native_runtime.checkpoint):
    directory = assets.safe_path(Path(directory))
    manifest = original.artifacts.verify(directory, check)
    original.require(
        manifest["version"] == VERSION + "-declaration"
        and set(manifest["files"]) == {"resolution.json"},
        "SOURCE_DECLARATION_DRIFT",
    )
    value = assets.read_json(directory / "resolution.json", check, maximum=original.MAX_UNIT)
    _verify_declaration(value)
    original.require(manifest["sources"] == value["execution"]["sources"])
    return value


def _verify_declaration(value):
    assets.verify_seal(value)
    original.require(
        value["version"] == VERSION
        and value["execution"] == execution()
        and value["cache_default"] is False
        and value["qualification_promoted"] is False,
        "SOURCE_EXECUTION_DRIFT",
    )
    original.validate_declaration(value["original"])
    original.require(
        value["original_manifest"]["content_hash"]
        == value["input_plan"]["original_source_manifest"],
        "SOURCE_OLD_PAYLOAD_DRIFT",
    )
    assets.verify_seal(value["original_manifest"])
    original.require(
        value["original_manifest"]["files"]["declaration.json"]
        == {
            "bytes": len(canonical(value["original"]) + b"\n"),
            "sha256": hashlib.sha256(canonical(value["original"]) + b"\n").hexdigest(),
        },
        "SOURCE_OLD_PAYLOAD_DRIFT",
    )


def _reader(source, work, value, check, *, cache=False):
    old = value["original"]
    receipt = assets.read_json(source / "acquisition.json", check, maximum=65536)
    assets.verify_seal(receipt)
    original.require(receipt == old["source_receipt"], "SOURCE_DRIFT")
    bare = source / "source.git"
    reader = Reader(
        bare,
        work,
        original.native_environment(work, bare),
        check,
        time.monotonic() + 30,
        source_id=value["content_hash"],
        cache=cache,
    )
    original.require(reader.command(["rev-parse", "--is-bare-repository"]).strip() == b"true")
    original.require(reader.command(["rev-parse", "--is-shallow-repository"]).strip() == b"false")
    return reader


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


def resolve(
    declaration,
    source,
    audit,
    cache,
    output,
    *,
    pilot=False,
    benchmark=None,
    check=native_runtime.checkpoint,
):
    paths = _separate(
        declaration, source, audit, cache, output, *([benchmark] if benchmark is not None else [])
    )
    declaration, source, audit, cache, output = paths[:5]
    original.require(type(pilot) is bool, "SOURCE_PILOT_INVALID")
    value = load_declaration(declaration, check)
    benchmark_manifest = None
    enabled = False
    if benchmark is not None:
        from packages.semantic import source_benchmark as measurement

        benchmark = paths[5]
        benchmark_manifest, benchmark_value = measurement.verify_benchmark(benchmark, check)
        original.require(
            benchmark_value["declaration_hash"] == value["content_hash"],
            "SOURCE_BENCHMARK_BINDING_DRIFT",
        )
        enabled = benchmark_value["decision"]["cache_enabled"]
    old = value["original"]
    original.require(original.artifacts.verify(audit, check) == value["original_manifest"])
    previous = _rows(audit / "observations.jsonl", check)
    _plan_matches(value["input_plan"], old, previous)
    original.require(original._inventory(source, check) == old["source_inventory"], "SOURCE_DRIFT")
    original_by_id = {row["metadata"]["commit_id"]: row for row in previous}
    planned = (
        [
            row
            for row in old["observations"]
            if row["commit_id"] in value["input_plan"]["pilot_observation_ids"]
        ]
        if pilot
        else old["observations"]
    )
    requested = set(
        value["input_plan"]["pilot_observation_ids" if pilot else "all_gap_observations"]
    )
    cache.mkdir(parents=True, exist_ok=True)
    observations = []
    with original.publication(cache), output_store(output, "resolve", check) as store:
        attempt = store.attempt
        with TemporaryDirectory(dir=attempt, prefix="native-") as temporary:
            work = assets.safe_path(Path(temporary))
            reader = _reader(source, work, value, check, cache=enabled)
            context = Context(
                old,
                source,
                cache,
                reader,
                check,
                binding=value["content_hash"],
                execution=value["execution"],
            )
            with store.lines("observations.jsonl") as stream:
                for i, expected in enumerate(planned):
                    check()
                    sha = expected["commit_id"]
                    row = (
                        {"kind": "resolved", "value": context.observation(expected)}
                        if sha in requested
                        else {"kind": "borrowed", "value": original_by_id[sha]}
                    )
                    raw = canonical(row) + b"\n"
                    store.append(stream, raw)
                    observations.append(row)
                    if sha in requested:
                        print(f"source resolution {i + 1}/{len(planned)} {sha}", flush=True)
            context.capacity(force=True)
        # TemporaryDirectory only owns this checked, private work directory.
        original.require(execution() == value["execution"], "SOURCE_EXECUTION_DRIFT")
        original.require(
            original._inventory(source, check) == old["source_inventory"], "SOURCE_DRIFT"
        )
        projected = (
            [] if pilot else original.outcomes(old, [_projection(row) for row in observations])
        )
        summary = _summary(value, observations, projected, pilot, cache_enabled=enabled)
        store.json("resolution.json", value)
        store.json("outcomes.json", {"records": projected})
        store.json("summary.json", summary)
        store.json(
            "cache-mode.json", {"enabled": enabled, "benchmark_manifest": benchmark_manifest}
        )
        benchmark_names = []
        if benchmark_manifest:
            for name in sorted(benchmark_manifest["files"]):
                copied_name = "cache-benchmark-" + name
                store.copy(copied_name, benchmark / name)
                benchmark_names.append(copied_name)
        # This canonical copy closes the borrowed evidence without depending on an
        # absolute old audit path; its exact original file digest remains bound.
        store.copy("original-observations.jsonl", audit / "observations.jsonl")
        original.require(
            assets.file_digest(
                attempt / "original-observations.jsonl", check, maximum=original.MAX_TREE
            )
            == value["original_manifest"]["files"]["observations.jsonl"],
            "SOURCE_OLD_PAYLOAD_DRIFT",
        )
        original.require(original.artifacts.verify(audit, check) == value["original_manifest"])
        original.require(load_declaration(declaration, check) == value, "SOURCE_DECLARATION_DRIFT")
        if benchmark_manifest:
            original.require(
                measurement.verify_benchmark(benchmark, check)[0] == benchmark_manifest,
                "SOURCE_BENCHMARK_BINDING_DRIFT",
            )
            # Validate the copied decision closure before publishing its consumer.
            measurement.verify_benchmark(
                attempt, check, manifest=benchmark_manifest, prefix="cache-benchmark-"
            )
        original.require(execution() == value["execution"], "SOURCE_EXECUTION_DRIFT")
        return store.publish(
            {
                "version": VERSION + ("-pilot" if pilot else "-audit"),
                "declaration_hash": value["content_hash"],
                "sources": value["execution"]["sources"],
                "assembled_at": datetime.now(UTC).isoformat(),
            },
            [
                "resolution.json",
                "original-observations.jsonl",
                "observations.jsonl",
                "outcomes.json",
                "summary.json",
                "cache-mode.json",
                *benchmark_names,
            ],
        )


def verify_audit(directory, check=native_runtime.checkpoint):
    directory = assets.safe_path(Path(directory))
    manifest = original.artifacts.verify(directory, check)
    original.require(
        manifest["version"] in {VERSION + "-audit", VERSION + "-pilot"}
        and sum(record["bytes"] for record in manifest["files"].values()) <= original.MAX_TREE,
        "SOURCE_AUDIT_INVALID",
    )
    value = assets.read_json(directory / "resolution.json", check, maximum=original.MAX_UNIT)
    _verify_declaration(value)
    mode = assets.read_json(directory / "cache-mode.json", check, maximum=original.MAX_UNIT)
    original.require(
        set(mode) == {"enabled", "benchmark_manifest"} and type(mode["enabled"]) is bool,
        "SOURCE_CACHE_MODE_INVALID",
    )
    extra_names = set()
    if mode["benchmark_manifest"] is not None:
        from packages.semantic import source_benchmark as measurement

        benchmark_manifest, benchmark_value = measurement.verify_benchmark(
            directory, check, manifest=mode["benchmark_manifest"], prefix="cache-benchmark-"
        )
        original.require(
            benchmark_value["declaration_hash"] == value["content_hash"]
            and mode["enabled"] is benchmark_value["decision"]["cache_enabled"],
            "SOURCE_CACHE_MODE_INVALID",
        )
        extra_names = {"cache-benchmark-" + name for name in benchmark_manifest["files"]}
    else:
        original.require(mode["enabled"] is False, "SOURCE_CACHE_MODE_INVALID")
    original.require(
        set(manifest["files"])
        == {
            "resolution.json",
            "original-observations.jsonl",
            "observations.jsonl",
            "outcomes.json",
            "summary.json",
            "cache-mode.json",
            *extra_names,
        },
        "SOURCE_AUDIT_INVALID",
    )
    original.require(
        manifest["sources"] == value["execution"]["sources"]
        and manifest["declaration_hash"] == value["content_hash"]
        and manifest["files"]["original-observations.jsonl"]
        == value["original_manifest"]["files"]["observations.jsonl"],
        "SOURCE_OLD_PAYLOAD_DRIFT",
    )
    old = value["original"]
    previous = _rows(directory / "original-observations.jsonl", check)
    _plan_matches(value["input_plan"], old, previous)
    by_id = {row["metadata"]["commit_id"]: row for row in previous}
    rows = _rows(directory / "observations.jsonl", check)
    pilot = manifest["version"] == VERSION + "-pilot"
    ids = (
        set(value["input_plan"]["pilot_observation_ids"])
        if pilot
        else {row["commit_id"] for row in old["observations"]}
    )
    requested = set(
        value["input_plan"]["pilot_observation_ids" if pilot else "all_gap_observations"]
    )
    expected_order = [row["commit_id"] for row in old["observations"] if row["commit_id"] in ids]
    original.require(
        [_projection(row)["metadata"]["commit_id"] for row in rows] == expected_order,
        "SOURCE_OBSERVATIONS_INCOMPLETE",
    )
    for row in rows:
        check()
        metadata = _projection(row)["metadata"]
        sha = metadata["commit_id"]
        original.require(metadata == by_id[sha]["metadata"], "SOURCE_OBSERVATION_DRIFT")
        if sha in requested:
            original.require(row["kind"] == "resolved", "SOURCE_GAP_NOT_ATTEMPTED")
            verify_unit(row["value"], old)
        else:
            original.require(
                row == {"kind": "borrowed", "value": by_id[sha]}, "SOURCE_BORROWED_EVIDENCE_DRIFT"
            )
            original.verify_observation(row["value"], old)
    projected = [] if pilot else original.outcomes(old, [_projection(row) for row in rows])
    actual = assets.read_json(directory / "outcomes.json", check, maximum=original.MAX_UNIT)
    original.require(actual == {"records": projected}, "SOURCE_OUTCOME_DRIFT")
    summary = assets.read_json(directory / "summary.json", check, maximum=original.MAX_UNIT)
    original.require(
        summary == _summary(value, rows, projected, pilot, cache_enabled=mode["enabled"]),
        "SOURCE_SUMMARY_DRIFT",
    )
    return manifest, summary


def summarize(directory, output, check=native_runtime.checkpoint):
    directory, output = _separate(directory, output)
    manifest, summary = verify_audit(directory, check)
    with output_store(output, "verify", check) as store:
        store.json("verification.json", {"audit": manifest["content_hash"], "summary": summary})
        return store.publish(
            {"version": VERSION + "-verification", "sources": manifest["sources"]},
            ["verification.json"],
        )
