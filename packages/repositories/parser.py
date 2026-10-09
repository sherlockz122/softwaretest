"""Read immutable bare objects with supervised Git; PyDriller sees bounded hunk text only."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from pydriller.domain.commit import ModifiedFile

from packages.repositories.clone import CloneExecutor, ExecutionStopped, terminate
from packages.repositories.parse_policy import (
    BATCH_SIZE,
    BLOB_LIMIT,
    DIFF_LIMIT,
    FILE_INDEX_LIMIT,
    IDENTITY_VERSION,
    INDEX_LIMIT,
    METADATA_LIMIT,
    PARSE_GROWTH,
    PARSER_VERSION,
)
from packages.repositories.safety import RepositoryError
from packages.repositories.service import RepositoryService

SHA = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def sha_valid(value):
    if not SHA.fullmatch(value):
        raise RepositoryError(422, "REPOSITORY_INVALID_METADATA")
    return value.decode("ascii")


def identity(header):
    match = re.fullmatch(rb"(.*) <([^<>]*)> (-?\d+) ([+-]\d{4})", header)
    if not match:
        raise RepositoryError(422, "REPOSITORY_INVALID_METADATA")
    name, email, seconds, offset = match.groups()
    try:
        timestamp = datetime.fromtimestamp(int(seconds), UTC).replace(tzinfo=None)
        if timestamp.year < 1000:
            raise ValueError
        hours, minutes = int(offset[1:3]), int(offset[3:])
        if hours > 23 or minutes > 59:
            raise ValueError
    except (ValueError, OverflowError, OSError):
        raise RepositoryError(422, "REPOSITORY_INVALID_METADATA") from None
    alias = name.decode("utf-8", "replace").strip()

    def normalized(value):
        try:
            return b"", value.decode("utf-8", "strict").strip().casefold().encode()
        except UnicodeError:
            # Replacement decoding must not silently merge different malformed identities.
            return b"raw:", value.strip().lower()

    encoding, normalized_email = normalized(email)
    email_hash = hashlib.sha256(normalized_email).hexdigest() if normalized_email else None
    if normalized_email:
        key = b"email:" + encoding + normalized_email
    else:
        encoding, normalized_name = normalized(name)
        key = b"name:" + encoding + normalized_name
    return (
        {
            "identity_key": hashlib.sha256(key).hexdigest(),
            "email_hash": email_hash,
            "name_alias": alias[:256],
            "identity_version": IDENTITY_VERSION,
        },
        timestamp,
        (hours * 60 + minutes) * (-1 if offset[:1] == b"-" else 1),
    )


class NativeReader:
    def __init__(self, bare, attempt, environment, tick, deadline, git=None):
        self.bare, self.attempt, self.environment = bare, attempt, environment
        self.tick, self.deadline, self.git = tick, deadline, git or shutil.which("git")

    def command(
        self, arguments, limit=METADATA_LIMIT, allow_limit=False, codes=(0,), exit_code=False
    ):
        output, errors = self.attempt / "parse.out", self.attempt / "parse.err"
        process = None
        try:
            with output.open("wb") as stdout, errors.open("wb") as stderr:
                process = subprocess.Popen(
                    [self.git, "--literal-pathspecs", "-C", str(self.bare), *arguments],
                    cwd=self.attempt,
                    env=self.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                while True:
                    self.tick()
                    if time.monotonic() >= self.deadline:
                        raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")
                    if errors.stat().st_size > 65536:
                        raise RepositoryError(422, "REPOSITORY_OUTPUT_LIMIT")
                    if output.stat().st_size > limit:
                        if allow_limit:
                            return None
                        raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
            if process.returncode not in codes:
                raise RepositoryError(422, "REPOSITORY_PARSE_FAILED")
            return process.returncode if exit_code else output.read_bytes()
        finally:
            if process is not None:
                terminate(process)
            output.unlink(missing_ok=True)
            errors.unlink(missing_ok=True)

    def ancestor(self, base, head):
        for value in (base, head):
            sha_valid(value.encode("ascii"))
        if self.command(["cat-file", "-e", base + "^{commit}"], codes=(0, 128), exit_code=True):
            return False
        return (
            self.command(["merge-base", "--is-ancestor", base, head], codes=(0, 1), exit_code=True)
            == 0
        )

    def plan(self, head, commit_limit, exclude=None):
        if head is None:
            return []
        sha_valid(head.encode("ascii"))
        revisions = [head]
        if exclude is not None:
            sha_valid(exclude.encode("ascii"))
            revisions.append("^" + exclude)
        raw = self.command(["rev-list", "--timestamp", *revisions, "--"], INDEX_LIMIT)
        rows = []
        for line in raw.splitlines():
            timestamp, sha = line.split(b" ", 1)
            rows.append((int(timestamp), sha_valid(sha)))
        ordered = [sha for _, sha in sorted(rows)]
        return ordered[-commit_limit:] if commit_limit is not None else ordered

    def blob(self, sha, mode):
        if set(sha) == {"0"} or mode == "160000":
            return b""
        size = int(self.command(["cat-file", "-s", sha]).strip())
        return None if size > BLOB_LIMIT else self.command(["cat-file", "blob", sha], BLOB_LIMIT)

    def record(self, sha):
        raw = self.command(["cat-file", "commit", sha])
        headers, separator, message = raw.partition(b"\n\n")
        if not separator:
            raise RepositoryError(422, "REPOSITORY_INVALID_METADATA")
        values = {}
        parents = []
        for line in headers.splitlines():
            if line.startswith(b" "):
                continue
            key, _, value = line.partition(b" ")
            if key == b"parent":
                parents.append(sha_valid(value))
            else:
                values[key] = value
        author, authored, author_offset = identity(values.get(b"author", b""))
        _, committed, committer_offset = identity(values.get(b"committer", b""))
        try:
            encoding = values.get(b"encoding", b"utf-8").decode("ascii")
            text = message.decode(encoding, "strict")
            if "\x00" in text:
                raise ValueError
            status = "parsed"
        except (UnicodeError, LookupError, ValueError):
            text, status = (
                message.decode("utf-8", "replace").replace("\x00", "\ufffd"),
                "message_encoding",
            )
        commit = {
            "sha": sha,
            "author_time": authored,
            "committer_time": committed,
            "author_offset": author_offset,
            "committer_offset": committer_offset,
            "message": text,
            "parents": parents,
            "parent_count": len(parents),
            "parse_status": "merge_skipped" if len(parents) > 1 else status,
            "parser_version": PARSER_VERSION,
        }
        files = [] if len(parents) > 1 else self.files(sha, parents)
        return {"identity": author, "commit": commit, "files": files}

    def files(self, sha, parents):
        common = [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--ignore-submodules=none",
            "-M50%",
            "-l1000",
        ]
        revisions = [parents[0], sha] if parents else [sha]
        raw = self.command(
            [*common, "--raw", "--no-abbrev", "-z", *revisions, "--"], FILE_INDEX_LIMIT
        )
        stats_raw = self.command([*common, "--numstat", "-z", *revisions, "--"], FILE_INDEX_LIMIT)
        stats, tokens, index = {}, stats_raw.split(b"\0"), 0
        while index < len(tokens) - 1:
            additions, deletions, path = tokens[index].split(b"\t", 2)
            index += 1
            if not path:
                old, path = tokens[index : index + 2]
                index += 2
            else:
                old = path
            stats[(old, path)] = (
                (None, None) if additions == b"-" else (int(additions), int(deletions))
            )
        parts, index, result, retained_bytes = raw.split(b"\0"), 0, [], 0
        while index < len(parts) - 1:
            if len(result) >= 4096:
                raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
            old_mode, new_mode, old_blob, new_blob, change = parts[index].split()
            old_mode, new_mode = old_mode[1:].decode(), new_mode.decode()
            old_blob, new_blob = sha_valid(old_blob), sha_valid(new_blob)
            old_path = new_path = parts[index + 1]
            index += 2
            kind = change[:1].decode("ascii")
            if kind in {"R", "C"}:
                new_path = parts[index]
                index += 1
            additions, deletions = stats[(old_path, new_path)]
            old_content, new_content = self.blob(old_blob, old_mode), self.blob(new_blob, new_mode)
            binary = additions is None
            status = "binary" if binary else "parsed"
            patch = numbers = old_loc = None
            if old_mode == "160000" or new_mode == "160000":
                status = "submodule"
            elif old_content is None or new_content is None:
                status = "blob_limit"
            elif not binary:
                old_loc = old_content.count(b"\n") + int(
                    bool(old_content) and not old_content.endswith(b"\n")
                )
                try:
                    old_path.decode("utf-8", "strict")
                    new_path.decode("utf-8", "strict")
                except UnicodeError:
                    status = "encoding"
                if status == "encoding":
                    patch_bytes = None
                else:
                    patch_bytes = self.command(
                        [
                            *common,
                            "-p",
                            "--unified=3",
                            *revisions,
                            "--",
                            os.fsdecode(old_path),
                            os.fsdecode(new_path),
                        ],
                        DIFF_LIMIT,
                        allow_limit=True,
                    )
                if patch_bytes is None:
                    status = "encoding" if status == "encoding" else "diff_limit"
                elif retained_bytes + len(patch_bytes) > METADATA_LIMIT:
                    status = "commit_diff_limit"
                elif len(re.findall(rb"^diff --git ", patch_bytes, re.M)) > 1:
                    status = "ambiguous_patch"
                else:
                    try:
                        patch = patch_bytes.decode("utf-8", "strict")
                        old_path.decode("utf-8", "strict")
                        new_path.decode("utf-8", "strict")
                        # PyDriller expects hunk text without ---/+++ file headers.
                        header = re.search(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", patch, re.M)
                        hunks = patch[header.start() :] if header else ""
                        lines = ModifiedFile(SimpleNamespace(diff=hunks)).diff_parsed
                        numbers = {
                            key: [number for number, _ in rows] for key, rows in lines.items()
                        }
                        retained_bytes += len(patch_bytes)
                    except UnicodeError:
                        patch, status = None, "encoding"
            result.append(
                {
                    "ordinal": len(result),
                    "old_path": None if kind == "A" else old_path.decode("utf-8", "replace"),
                    "new_path": None if kind == "D" else new_path.decode("utf-8", "replace"),
                    "path_bytes_hash": hashlib.sha256(old_path + b"\0" + new_path).hexdigest(),
                    "old_blob": old_blob,
                    "new_blob": new_blob,
                    "change_type": kind,
                    "insertions": additions,
                    "deletions": deletions,
                    "old_loc": old_loc,
                    "is_binary": binary,
                    "content_status": status,
                    "diff_text": patch,
                    "line_numbers": numbers,
                    "parser_version": PARSER_VERSION,
                }
            )
        return result


class ParseExecutor:
    def __init__(self, settings, service):
        self.settings, self.service, self.storage = settings, service, service.storage

    def run(self, task_id, token, payload):
        attempt = None
        try:
            self.storage.capacity(PARSE_GROWTH)
            source = self.service.source(task_id, token)
            if source is None:
                return False
            bare, head, commit_limit = source
            attempt = self.storage.attempt(payload["repository_id"], str(uuid4()))
            clone = CloneExecutor(
                self.settings,
                RepositoryService(self.settings, SimpleNamespace(engine=self.service.engine)),
            )
            if not clone.git:
                raise RepositoryError(503, "REPOSITORY_GIT_UNAVAILABLE")
            environment, _ = clone.environment(attempt, SimpleNamespace(port=0))
            environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
            configs = {
                "protocol.https.allow": "never",
                "diff.external": "",
                "core.attributesFile": os.devnull,
            }
            start = int(environment["GIT_CONFIG_COUNT"])
            for index, (key, value) in enumerate(configs.items(), start):
                environment[f"GIT_CONFIG_KEY_{index}"] = key
                environment[f"GIT_CONFIG_VALUE_{index}"] = value
            environment["GIT_CONFIG_COUNT"] = str(start + len(configs))
            processed, total, heartbeat = 0, None, 0

            def tick():
                nonlocal heartbeat
                self.storage.capacity(PARSE_GROWTH)
                if time.monotonic() - heartbeat >= min(1, self.settings.task_heartbeat_seconds):
                    if not self.service.heartbeat(task_id, token):
                        raise ExecutionStopped
                    heartbeat = time.monotonic()

            reader = NativeReader(
                bare,
                attempt,
                environment,
                tick,
                time.monotonic() + self.settings.repository_parse_timeout_seconds,
                clone.git,
            )
            plan = self.service.plan(task_id, token, reader, head, commit_limit)
            plan_hash = hashlib.sha256(json.dumps(plan, separators=(",", ":")).encode()).hexdigest()
            total = len(plan)
            resume = self.service.prepare(task_id, token, plan_hash, total)
            if resume is None:
                return False
            processed, last_sha = resume
            if processed and plan[processed - 1] != last_sha:
                raise RepositoryError(409, "REPOSITORY_PARSE_PLAN_CONFLICT")
            while processed < total:
                records = []
                for sha in plan[processed : processed + BATCH_SIZE]:
                    tick()
                    records.append(self.service.record(task_id, token, reader, sha))
                if not self.service.batch(
                    task_id,
                    token,
                    processed,
                    records,
                    plan_hash,
                    complete=processed + len(records) == total,
                ):
                    return False
                processed += len(records)
            if not total or processed == resume[0]:
                return self.service.batch(task_id, token, processed, [], plan_hash, complete=True)
            return True
        except ExecutionStopped:
            return False
        except RepositoryError as error:
            RepositoryService(self.settings, SimpleNamespace(engine=self.service.engine)).fail(
                task_id, token, error.code
            )
            return False
        finally:
            if attempt is not None:
                self.storage.remove(attempt)
