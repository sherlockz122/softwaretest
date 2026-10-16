"""Real first-parent prediction-side text; no fixes, hidden fetches or label repair."""

import base64
import csv
import difflib
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.research_dataset import CSV_FIELDS, PARTITIONS
from packages.mining.source_evidence import canonical, content_hash
from packages.repositories.parser import NativeReader, sha_valid
from packages.repositories.safety import RepositoryError
from packages.semantic import artifacts, policy
from packages.semantic.policy import SemanticError


def native_environment(home, bare=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home),
        TEMP=str(home),
        TMP=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    config = {
        "protocol.allow": "never",
        "core.hooksPath": str(home),
        "core.fsmonitor": "false",
        "core.longpaths": "true",
    }
    if bare is not None:
        # Trust only this verified local acquisition, without changing global Git/ACLs.
        config["safe.directory"] = str(assets.safe_path(Path(bare)))
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
    return env


def object_hash(kind, body):
    return hashlib.sha1(f"{kind} {len(body)}\0".encode() + body).hexdigest()


def text(body):
    if b"\0" in body:
        raise SemanticError("SEMANTIC_BINARY")
    try:
        result = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise SemanticError("SEMANTIC_ENCODING") from None
    if len(result.splitlines()) > 5000:
        raise SemanticError("SEMANTIC_LINE_LIMIT")
    return result


def changed_lines(before, after):
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    added, deleted = [], []
    for tag, a, b, c, d in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in {"replace", "delete"}:
            deleted.extend(old[a:b])
        if tag in {"replace", "insert"}:
            added.extend(new[c:d])
    return "".join(added), "".join(deleted)


def licenses(reader, sha):
    cache = getattr(reader, "semantic_licenses", {})
    reader.semantic_licenses = cache
    entries = reader.command(
        [
            "ls-tree",
            "-z",
            sha,
            "--",
            "LICENSE",
            "LICENSE.txt",
            "LICENSE.md",
            "NOTICE",
            "NOTICE.txt",
            "NOTICE.md",
        ],
        limit=16384,
    )
    refs = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        info, name = entry.split(b"\t", 1)
        mode, kind, blob = info.split()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise SemanticError("SEMANTIC_LICENSE_INVALID")
        identity = sha_valid(blob)
        if identity not in cache:
            body = reader.command(["cat-file", "blob", identity], limit=512 * 1024)
            if object_hash("blob", body) != identity:
                raise SemanticError("SEMANTIC_OBJECT_DRIFT")
            cache[identity] = {
                "original_bytes_base64": base64.b64encode(body).decode("ascii"),
                "sha256": hashlib.sha256(body).hexdigest(),
                "apache_header_present": b"Apache License" in body
                or b"Apache Software License" in body,
                "scope": (
                    "unaltered license document, not model input or blanket redistribution approval"
                ),
            }
        refs.append({"path": name.decode("ascii"), "git_blob": identity})
    if not any(r["path"] in {"LICENSE", "LICENSE.txt", "LICENSE.md"} for r in refs):
        raise SemanticError("SEMANTIC_LICENSE_MISSING")
    return refs


def extract_commit(reader, row):
    sha = sha_valid(row["commit_id"].encode("ascii"))
    reader.deadline = time.monotonic() + 30
    if reader.command(["cat-file", "-e", sha + "^{commit}"], codes=(0, 128), exit_code=True):
        raise SemanticError("SEMANTIC_OBJECT_MISSING")
    raw = reader.command(["cat-file", "commit", sha], limit=65536)
    if object_hash("commit", raw) != sha:
        raise SemanticError("SEMANTIC_OBJECT_DRIFT")
    header, message = raw.split(b"\n\n", 1)
    headers = defaultdict(list)
    for line in header.splitlines():
        if line and not line.startswith(b" "):
            key, _, value = line.partition(b" ")
            headers[key].append(value)
    if len(headers[b"parent"]) != 1:
        raise SemanticError("SEMANTIC_PARENT_UNUSABLE")
    if headers.get(b"encoding", [b"UTF-8"])[0].lower() not in {b"utf-8", b"utf8", b"us-ascii"}:
        raise SemanticError("SEMANTIC_ENCODING")
    if len(message) > 16384:
        raise SemanticError("SEMANTIC_MESSAGE_LIMIT")
    author_epoch = int(headers[b"author"][0].rsplit(b" ", 2)[1])
    if author_epoch != int(row["event_epoch"]):
        raise SemanticError("SEMANTIC_EVENT_CONFLICT")
    parent = sha_valid(headers[b"parent"][0])
    license_refs = licenses(reader, sha)
    if reader.command(["cat-file", "-e", parent + "^{commit}"], codes=(0, 128), exit_code=True):
        raise SemanticError("SEMANTIC_PARENT_MISSING")
    raw_diff = reader.command(
        [
            "diff-tree",
            "--no-commit-id",
            "--raw",
            "-r",
            "-z",
            "--no-renames",
            "--no-abbrev",
            "--no-ext-diff",
            parent,
            sha,
        ],
        limit=65536,
    )
    parts = raw_diff.split(b"\0")
    if parts[-1] != b"" or (len(parts) - 1) % 2:
        raise SemanticError("SEMANTIC_DIFF_INVALID")
    if not 1 <= (len(parts) - 1) // 2 <= policy.MAX_FILES:
        raise SemanticError("SEMANTIC_FILE_LIMIT")
    files, added, deleted, bases = [], [], [], []
    for index in range(0, len(parts) - 1, 2):
        info, path = parts[index], parts[index + 1]
        modes = info.removeprefix(b":").split()
        if len(modes) != 5 or any(m not in {b"000000", b"100644", b"100755"} for m in modes[:2]):
            raise SemanticError("SEMANTIC_FILE_MODE")
        if len(path) > 4096:
            raise SemanticError("SEMANTIC_PATH_LIMIT")
        name = text(path)
        bodies = []
        for blob in modes[2:4]:
            sha_valid(blob)
            if blob == b"0" * 40:
                bodies.append(b"")
                continue
            body = reader.command(
                ["cat-file", "blob", blob.decode()], limit=policy.MAX_BLOB, allow_limit=True
            )
            if body is None:
                raise SemanticError("SEMANTIC_BLOB_LIMIT")
            if object_hash("blob", body) != blob.decode():
                raise SemanticError("SEMANTIC_OBJECT_DRIFT")
            bodies.append(body)
        before, after = map(text, bodies)
        a, d = changed_lines(before, after)
        reader.tick()
        if time.monotonic() > reader.deadline:
            raise SemanticError("SEMANTIC_COMMIT_DEADLINE")
        added.append(a)
        deleted.append(d)
        bases.append(before)
        extension = Path(name).suffix.lower()
        files.append(
            {
                "path": name,
                "old_blob": modes[2].decode(),
                "new_blob": modes[3].decode(),
                "before_bytes": len(bodies[0]),
                "after_bytes": len(bodies[1]),
                "extension": extension or "none",
                "language_hint": "Java" if extension == ".java" else "unknown",
            }
        )
    roles = {
        "message": text(message),
        "added": "\n".join(added),
        "deleted": "\n".join(deleted),
        "base": "\n".join(bases),
    }
    if sum(len(v.encode()) for v in (roles["added"], roles["deleted"])) > policy.MAX_PATCH:
        raise SemanticError("SEMANTIC_PATCH_LIMIT")
    if sum(len(v.encode()) for v in roles.values()) > policy.MAX_RECORD:
        raise SemanticError("SEMANTIC_RECORD_LIMIT")
    return {
        "member": row,
        "parent": parent,
        "roles": roles,
        "files": files,
        "text_hash": content_hash(roles),
        "code_hash": content_hash({k: v for k, v in roles.items() if k != "message"}),
        "author_epoch": author_epoch,
        "source_side": "commit and immediate parent only",
        "root_license_refs": license_refs,
    }


def extract(dataset, source, output, check=lambda: None):
    dataset, source, output = map(lambda p: assets.safe_path(Path(p)), (dataset, source, output))
    receipt = assets.read_json(source / "acquisition.json", check)
    assets.verify_seal(receipt)
    if receipt["repository"] != policy.REPOSITORY:
        raise SemanticError("SEMANTIC_REPOSITORY_CONFLICT")
    frozen = assets.read_json(dataset / "manifest.json", check, maximum=1024**2)
    assets.verify_seal(frozen)
    rows = []
    for part in PARTITIONS:
        path = dataset / (part + ".csv")
        if artifacts.file_digest(path, check) != frozen["files"][part]:
            raise SemanticError("SEMANTIC_DATASET_DRIFT")
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise SemanticError("SEMANTIC_DATASET_SCHEMA")
            selected = [r for r in reader if r["canonical_repository"] == policy.REPOSITORY]
        if part == "train":
            selected = sorted(selected, key=lambda r: r["commit_id"])[:512]
        rows.extend(r | {"partition": part} for r in selected)
    if len(rows) > 2000:
        raise SemanticError("SEMANTIC_POPULATION_LIMIT")
    output.mkdir(parents=True, exist_ok=True)
    attempt = assets.safe_path(output / (".extract-" + uuid4().hex))
    attempt.mkdir()
    bare = source / "source.git"
    if any(
        (bare / p).exists()
        for p in ["objects/info/alternates", "objects/info/http-alternates", "info/grafts"]
    ):
        raise SemanticError("SEMANTIC_GIT_ALTERNATES")
    accepted, rejected = [], []
    with TemporaryDirectory(prefix="native-", dir=attempt) as work:
        home = Path(work)
        reader = NativeReader(
            bare, home, native_environment(home, bare), check, time.monotonic() + 30
        )
        if reader.command(["rev-parse", "--is-bare-repository"], limit=1024).strip() != b"true":
            raise SemanticError("SEMANTIC_GIT_STORAGE_INVALID")
        for index, row in enumerate(rows):
            check()
            try:
                accepted.append(extract_commit(reader, row))
            except (SemanticError, RepositoryError) as exc:
                code = str(exc) if isinstance(exc, SemanticError) else exc.code
                rejected.append({"member": row, "reason": code})
            if index % 50 == 0:
                print(
                    json.dumps(
                        {
                            "extract_processed": index + 1,
                            "selected": len(rows),
                            "accepted": len(accepted),
                        }
                    ),
                    flush=True,
                )
    groups = defaultdict(list)
    for record in accepted:
        for kind in ("text_hash", "code_hash"):
            groups[(kind, record[kind])].append(record)
    duplicate_ids = set()
    for group in groups.values():
        if len({r["member"]["partition"] for r in group}) > 1:
            duplicate_ids.update(r["member"]["commit_id"] for r in group)
    final = []
    for record in accepted:
        if record["member"]["commit_id"] in duplicate_ids:
            rejected.append(
                {"member": record["member"], "reason": "SEMANTIC_CROSS_SPLIT_DUPLICATE"}
            )
        else:
            final.append(record)
    final.sort(
        key=lambda r: (
            PARTITIONS.index(r["member"]["partition"]),
            int(r["member"]["event_epoch"]),
            r["member"]["commit_id"],
        )
    )
    with (attempt / "records.jsonl").open("xb") as stream:
        for record in final:
            stream.write(canonical(record) + b"\n")
    assets.write_json(
        attempt / "licenses.json",
        {
            "documents": getattr(reader, "semantic_licenses", {}),
            "scope": (
                "original root notices at each commit; individual file/vendor "
                "obligations retained; no redistribution approval"
            ),
        },
    )
    assets.write_json(attempt / "rejected.json", {"records": rejected})
    supports = {
        p: dict(Counter(r["member"]["label"] for r in final if r["member"]["partition"] == p))
        for p in PARTITIONS
    }
    audit = {
        "extractor_sources": artifacts.sources(
            [
                "packages/semantic/git_inputs.py",
                "packages/semantic/policy.py",
                "packages/semantic/artifacts.py",
                "packages/repositories/parser.py",
            ]
        ),
        "version": policy.VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source": receipt,
        "frozen_dataset": frozen,
        "selection": {"train": "512 smallest SHA, label-blind", "validation": "all", "test": "all"},
        "selected": len(rows),
        "accepted": len(final),
        "supports": supports,
        "reasons": dict(Counter(r["reason"] for r in rejected)),
        "language_hints": dict(Counter(f["language_hint"] for r in final for f in r["files"])),
        "cross_split_duplicate_members_removed": len(duplicate_ids),
        "research_only": True,
        "capabilities": {
            "historical_replay": False,
            "candidate_release": False,
            "type_prediction": False,
            "language_effectiveness": False,
        },
    }
    return artifacts.publish(
        attempt, output, audit, ["records.jsonl", "rejected.json", "licenses.json"]
    )
