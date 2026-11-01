"""Code-approved offline Git material, with no supplied configuration or network execution."""

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psutil

from packages.mining import public_assets as assets
from packages.repositories.storage import filesystem_path
from packages.repositories.storage import safe_path as storage_path
from packages.semantic import artifacts, native_runtime, supplement_policy, supplement_source
from packages.semantic.admission_git import native_environment, object_hash
from packages.semantic.policy import SemanticError

VERSION = "fixed-offline-source-v3"
BUNDLE_NAME = "apache-commons-fixed-source-bundle-v1"
ARCHIVE_SHA256 = "f3e035dc9a82982d492a2a82808f8375976c189ebc6463e139871d54905b0d50"
SOURCE_PAIRS = (
    ("apache/commons-collections", "5fc937c676f9c3dfe018db7cae9a2d57d33cae4e"),
    ("apache/commons-codec", "315fd72b289a0c47adef636acb637517399a2dfe"),
)
MAX_SECONDS = 600
MAX_ARCHIVE_BYTES = 128 * 1024**2
MAX_EXTRACTED_BYTES = 512 * 1024**2
MAX_MEMBER_BYTES = 256 * 1024**2
MAX_MEMBERS = 1024
MAX_GIT_OUTPUT = 8 * 1024**2
MAX_RSS = 1024**3
MAX_METADATA = 2 * 1024**2
BARE_CONFIG = b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
LICENSES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "NOTICE.txt", "NOTICE.md")
BOUND_FILES = (
    "packages/semantic/offline_source.py",
    "scripts/semantic_source_supplement.py",
    *supplement_source.BINDINGS,
    "packages/mining/source_evidence.py",
)


def require(condition, code):
    if not condition:
        raise SemanticError(code)


def bindings():
    return artifacts.sources(BOUND_FILES)


def regular(path):
    storage_path(path)
    info = filesystem_path(path).lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "OFFLINE_FILE_INVALID")
    require(not getattr(info, "st_file_attributes", 0) & 0x400, "OFFLINE_FILE_INVALID")
    return info


def fingerprint(path, check, maximum, *, runtime=True):
    path = assets.safe_path(path) if runtime else Path(path)
    before = regular(path) if runtime else path.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, "OFFLINE_FILE_LIMIT")
    digest, count = hashlib.sha256(), 0
    with filesystem_path(path).open("rb") as stream:
        opened = os.fstat(stream.fileno())
        require(
            (opened.st_ino, opened.st_size) == (before.st_ino, before.st_size),
            "OFFLINE_INPUT_DRIFT",
        )
        while body := stream.read(1024 * 1024):
            check()
            count += len(body)
            require(count <= maximum, "OFFLINE_FILE_LIMIT")
            digest.update(body)
        after = os.fstat(stream.fileno())
    current = filesystem_path(path).stat()
    require(
        all(
            (s.st_ino, s.st_size, s.st_mtime_ns)
            == (before.st_ino, before.st_size, before.st_mtime_ns)
            for s in (after, current)
        )
        and count == before.st_size,
        "OFFLINE_INPUT_DRIFT",
    )
    check()
    return {"bytes": count, "sha256": digest.hexdigest()}


def inventory(root, check, *, exclude=()):
    root = assets.safe_path(root)
    result, total = {}, 0

    def unreadable(error):
        raise SemanticError("OFFLINE_INVENTORY_UNREADABLE") from error

    for current, dirs, files in os.walk(
        filesystem_path(root), followlinks=False, onerror=unreadable
    ):
        if current.startswith("\\\\?\\"):
            current = current[4:]
        check()
        for name in dirs:
            path = assets.safe_path(Path(current) / name)
            storage_path(path)
            require(stat.S_ISDIR(filesystem_path(path).lstat().st_mode), "OFFLINE_FILE_INVALID")
        for name in files:
            path = assets.safe_path(Path(current) / name)
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            require(len(result) < 8192, "OFFLINE_INVENTORY_LIMIT")
            row = fingerprint(path, check, MAX_MEMBER_BYTES)
            total += row["bytes"]
            require(total <= 1024**3, "OFFLINE_OUTPUT_LIMIT")
            result[relative] = row
    return result


class Budget:
    def __init__(self, root, check):
        self.root, self.check = assets.safe_path(root), check
        self.deadline = time.monotonic() + MAX_SECONDS
        self.commands, self.identities, self.peak = [], {}, 0
        self.tick()
        self.git = shutil.which("git")
        require(self.git is not None, "OFFLINE_GIT_MISSING")
        self.git_identity = fingerprint(Path(self.git), self.tick, 64 * 1024**2, runtime=False)

    def tick(self):
        require(time.monotonic() < self.deadline, "OFFLINE_TIMEOUT")
        require(shutil.disk_usage(assets.ROOT).free >= 2.5 * 1024**3, "OFFLINE_LOW_STORAGE")
        require(psutil.Process().memory_info().rss <= MAX_RSS, "OFFLINE_PROCESS_MEMORY")
        self.check()

    def observe(self, pid, created, descendants):
        rss = 0
        for identity, stamp in ({pid: created} | descendants).items():
            if stamp is None:
                continue
            self.identities[identity, stamp] = True
            try:
                process = psutil.Process(identity)
                if process.create_time() == stamp:
                    rss += process.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        self.peak = max(self.peak, rss)
        require(rss <= MAX_RSS, "OFFLINE_PROCESS_MEMORY")

    def command(self, bare, home, arguments, name, *, limit=65536, seconds=60):
        self.tick()
        home = assets.safe_path(home)
        home.mkdir(parents=True, exist_ok=True)
        hooks = home / "empty-hooks"
        hooks.mkdir(exist_ok=True)
        env = native_environment(home, bare)
        env.update(GIT_ALLOW_PROTOCOL="", GIT_ATTR_NOSYSTEM="1", GIT_LFS_SKIP_SMUDGE="1")
        for i in range(int(env["GIT_CONFIG_COUNT"])):
            if env[f"GIT_CONFIG_KEY_{i}"] == "core.hooksPath":
                env[f"GIT_CONFIG_VALUE_{i}"] = str(hooks)
        record = {"phase": name}

        def capture(stdout, stderr, truncated):
            record.update(
                stdout_bytes=len(stdout),
                stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                stderr_bytes=len(stderr),
                stderr_sha256=hashlib.sha256(stderr).hexdigest(),
                truncated=list(truncated),
            )
            prefix = home / f"{len(self.commands):04d}"
            prefix.with_suffix(".stdout").write_bytes(stdout)
            prefix.with_suffix(".stderr").write_bytes(stderr)

        try:
            result = native_runtime.run(
                [self.git, "--literal-pathspecs", "-C", str(bare), *arguments],
                directory=home,
                environment=env,
                check=self.tick,
                deadline=min(self.deadline, time.monotonic() + seconds),
                limit=min(limit, MAX_GIT_OUTPUT),
                observe=self.observe,
                capture=capture,
            )
            require(result.cleanup_complete, "OFFLINE_PROCESS_CLEANUP")
            record.update(
                cleanup_complete=True, seconds=result.seconds, returncode=result.returncode
            )
            return result.data
        finally:
            self.commands.append(record)

    def final(self):
        self.tick()
        require(
            not any(native_runtime._live(pid, stamp) for pid, stamp in self.identities),
            "OFFLINE_PROCESS_CLEANUP",
        )
        require(
            fingerprint(Path(self.git), self.tick, 64 * 1024**2, runtime=False)
            == self.git_identity,
            "OFFLINE_EXECUTION_DRIFT",
        )


def member_name(value):
    require(
        type(value) is str
        and 0 < len(value) <= 512
        and "\\" not in value
        and "\0" not in value
        and not value.startswith("/"),
        "OFFLINE_ARCHIVE_PATH",
    )
    parts = value.rstrip("/").split("/")
    require(parts[0] == BUNDLE_NAME and len(parts) > 1, "OFFLINE_ARCHIVE_PATH")
    for part in parts:
        require(
            part not in {"", ".", ".."}
            and not any(c in part for c in ':<>"|?*')
            and all(ord(c) >= 32 for c in part)
            and not part.endswith((".", " "))
            and re.fullmatch(
                r"(?i)(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?",
                part,
            )
            is None,
            "OFFLINE_ARCHIVE_PATH",
        )
    return "/".join(parts[1:])


def json_data(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "OFFLINE_JSON_DUPLICATE")
            result[key] = value
        return result

    require(len(raw) <= MAX_METADATA, "OFFLINE_METADATA_LIMIT")
    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: require(False, "OFFLINE_JSON_INVALID"),
    )


def inspect_archive(archive, budget, destination=None):
    archive = assets.safe_path(archive)
    actual = fingerprint(archive, budget.tick, MAX_ARCHIVE_BYTES)
    require(actual["sha256"] == ARCHIVE_SHA256, "OFFLINE_ARCHIVE_IDENTITY")
    # Bound allocation before ZipFile parses entries; no ZIP64 or multidisk archives.
    with archive.open("rb") as stream:
        stream.seek(max(0, actual["bytes"] - 65557))
        tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        require(offset >= 0 and len(tail) - offset >= 22, "OFFLINE_ARCHIVE_FORMAT")
        _, disk, start_disk, disk_entries, entries, size, start, comment = struct.unpack(
            "<4s4H2LH", tail[offset : offset + 22]
        )
        require(
            disk == start_disk == 0
            and disk_entries == entries
            and 0 < entries <= MAX_MEMBERS
            and size <= MAX_METADATA
            and start + size == actual["bytes"] - len(tail) + offset
            and len(tail) - offset == 22 + comment,
            "OFFLINE_ARCHIVE_FORMAT",
        )
        stream.seek(0)
        with zipfile.ZipFile(stream) as zipped:
            infos = zipped.infolist()
            require(len(infos) == entries, "OFFLINE_ARCHIVE_FORMAT")
            names, total = {}, 0
            for info in infos:
                budget.tick()
                name = member_name(info.orig_filename)
                require(info.orig_filename == info.filename, "OFFLINE_ARCHIVE_PATH")
                folded = name.casefold()
                require(folded not in names, "OFFLINE_ARCHIVE_COLLISION")
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                require(
                    kind in {0, stat.S_IFREG, stat.S_IFDIR}
                    and not info.flag_bits & 1
                    and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    and 0 <= info.file_size <= MAX_MEMBER_BYTES,
                    "OFFLINE_ARCHIVE_MODE_OR_LIMIT",
                )
                require(not info.is_dir() or kind in {0, stat.S_IFDIR}, "OFFLINE_ARCHIVE_MODE")
                total += info.file_size
                require(total <= MAX_EXTRACTED_BYTES, "OFFLINE_ARCHIVE_TOTAL_LIMIT")
                names[folded] = (name, info)
            for folded in names:
                for parent in Path(folded).parents:
                    key = parent.as_posix()
                    require(key not in names or names[key][1].is_dir(), "OFFLINE_ARCHIVE_COLLISION")
            checksum_entry = names.get("sha256sums.txt")
            require(
                checksum_entry is not None
                and not checksum_entry[1].is_dir()
                and checksum_entry[1].file_size <= 256 * 1024,
                "OFFLINE_CHECKSUM_COVERAGE",
            )
            checksums = {}
            for line in zipped.read(checksum_entry[1]).decode("utf-8").splitlines():
                digest, separator, name = line.partition("  ")
                require(
                    separator
                    and re.fullmatch("[0-9a-f]{64}", digest)
                    and member_name(BUNDLE_NAME + "/" + name) == name
                    and name not in checksums,
                    "OFFLINE_CHECKSUM_INVALID",
                )
                checksums[name] = digest
            files = {name for name, info in names.values() if not info.is_dir()}
            require(set(checksums) == files - {"SHA256SUMS.txt"}, "OFFLINE_CHECKSUM_COVERAGE")
            records, manifest_body = {}, None
            for name, info in names.values():
                if info.is_dir():
                    continue
                budget.tick()
                target = None
                if destination is not None:
                    target = assets.safe_path(destination / name)
                    storage_path(target)
                    filesystem_path(target.parent).mkdir(parents=True, exist_ok=True)
                digest, count, chunks = hashlib.sha256(), 0, []
                with zipped.open(info) as source:
                    sink = filesystem_path(target).open("xb") if target is not None else None
                    try:
                        while chunk := source.read(1024 * 1024):
                            budget.tick()
                            count += len(chunk)
                            require(count <= info.file_size, "OFFLINE_ARCHIVE_SIZE_DRIFT")
                            digest.update(chunk)
                            if sink is not None:
                                sink.write(chunk)
                            if name == "manifest.json":
                                require(count <= MAX_METADATA, "OFFLINE_METADATA_LIMIT")
                                chunks.append(chunk)
                    finally:
                        if sink is not None:
                            sink.close()
                require(count == info.file_size, "OFFLINE_ARCHIVE_SIZE_DRIFT")
                records[name] = {"bytes": count, "sha256": digest.hexdigest()}
                require(
                    name == "SHA256SUMS.txt" or records[name]["sha256"] == checksums[name],
                    "OFFLINE_CHECKSUM_MISMATCH",
                )
                if name == "manifest.json":
                    manifest_body = b"".join(chunks)
    require(fingerprint(archive, budget.tick, MAX_ARCHIVE_BYTES) == actual, "OFFLINE_INPUT_DRIFT")
    require(manifest_body is not None, "OFFLINE_MANIFEST_MISSING")
    manifest = json_data(manifest_body)
    require(
        type(manifest) is dict
        and type(manifest.get("sources")) is list
        and manifest.get("source_count") == len(SOURCE_PAIRS)
        and len(manifest["sources"]) == len(SOURCE_PAIRS),
        "OFFLINE_SOURCE_IDENTITY",
    )
    pairs = set()
    for source in manifest["sources"]:
        require(type(source) is dict, "OFFLINE_SOURCE_IDENTITY")
        pair = (source.get("name"), source.get("pinned_commit"))
        require(pair in SOURCE_PAIRS and pair not in pairs, "OFFLINE_SOURCE_IDENTITY")
        approved = supplement_policy.approved_source(*pair)
        require(source.get("official_url") == approved["url"], "OFFLINE_SOURCE_IDENTITY")
        assets.utc_time(source.get("acquired_utc"))
        pairs.add(pair)
    require(pairs == set(SOURCE_PAIRS), "OFFLINE_SOURCE_IDENTITY")
    return actual, records, manifest


def validate_layout(bare, budget):
    for name in (
        "shallow",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "refs/replace",
    ):
        require(not filesystem_path(bare / name).exists(), "OFFLINE_SOURCE_LAYOUT")
    records = inventory(bare, budget.tick)
    require(not any(n.endswith(".promisor") for n in records), "OFFLINE_SOURCE_LAYOUT")
    return records


def clean_copy(raw, bare, head, budget):
    validate_layout(raw, budget)
    raw_inventory = inventory(raw, budget.tick)
    for name in raw_inventory:
        require(
            name in {"HEAD", "config", "description", "info/exclude", "refs/heads/pinned"}
            or re.fullmatch(r"hooks/[-a-z]+\.sample", name)
            or re.fullmatch(r"objects/pack/pack-[a-f0-9]{40}\.(pack|idx|rev)", name),
            "OFFLINE_SOURCE_LAYOUT",
        )
    require(
        (raw / "HEAD").read_bytes() == b"ref: refs/heads/pinned\n"
        and (raw / "refs/heads/pinned").read_bytes() == (head + "\n").encode(),
        "OFFLINE_SOURCE_IDENTITY",
    )
    (bare / "objects/pack").mkdir(parents=True)
    (bare / "refs/heads").mkdir(parents=True)
    (bare / "HEAD").write_bytes(b"ref: refs/heads/pinned\n")
    (bare / "config").write_bytes(BARE_CONFIG)
    (bare / "refs/heads/pinned").write_bytes((head + "\n").encode())
    packs = [name for name in raw_inventory if name.endswith(".pack")]
    require(
        packs and all(name[:-5] + ".idx" in raw_inventory for name in packs),
        "OFFLINE_SOURCE_PACK_MISSING",
    )
    for name, expected in raw_inventory.items():
        if name.startswith("objects/pack/"):
            require(
                name.rsplit(".", 1)[0] + ".pack" in raw_inventory, "OFFLINE_SOURCE_PACK_MISSING"
            )
            target = bare / name
            with (
                filesystem_path(raw / name).open("rb") as source,
                filesystem_path(target).open("xb") as sink,
            ):
                while chunk := source.read(1024 * 1024):
                    budget.tick()
                    sink.write(chunk)
            require(
                fingerprint(target, budget.tick, MAX_MEMBER_BYTES) == expected,
                "OFFLINE_INPUT_DRIFT",
            )


def native_facts(bare, home, raw, expected, budget):
    validate_layout(bare, budget)
    require((bare / "config").read_bytes() == BARE_CONFIG, "OFFLINE_GENERATED_CONFIG_DRIFT")
    head, repository = expected["pinned_commit"], expected["name"]

    def run(args, name, limit=65536, seconds=60):
        return budget.command(bare, home, args, name, limit=limit, seconds=seconds)

    require(
        run(["rev-parse", "--is-bare-repository"], "bare").strip() == b"true",
        "OFFLINE_SOURCE_LAYOUT",
    )
    require(
        run(["rev-parse", "--is-shallow-repository"], "shallow").strip() == b"false",
        "OFFLINE_SOURCE_LAYOUT",
    )
    require(
        run(["rev-parse", "HEAD", "refs/heads/pinned"], "head").splitlines() == [head.encode()] * 2,
        "OFFLINE_SOURCE_IDENTITY",
    )
    require(
        run(["for-each-ref", "--format=%(refname) %(objectname)"], "refs").strip()
        == ("refs/heads/pinned " + head).encode(),
        "OFFLINE_SOURCE_IDENTITY",
    )
    require(run(["remote"], "remotes") == b"", "OFFLINE_SOURCE_LAYOUT")
    require(
        run(["fsck", "--full", "--strict", "--no-reflogs"], "fsck", seconds=120) == b"",
        "OFFLINE_OBJECT_CLOSURE",
    )
    reachable = run(
        ["rev-list", "--objects", "--no-object-names", "--missing=print", head],
        "reachable",
        MAX_GIT_OUTPUT,
    ).splitlines()
    require(
        0 < len(reachable) <= 200000
        and len(set(reachable)) == len(reachable)
        and all(re.fullmatch(b"[a-f0-9]{40}", oid) for oid in reachable),
        "OFFLINE_OBJECT_CLOSURE",
    )
    objects = run(
        [
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        ],
        "all-objects",
        MAX_GIT_OUTPUT,
    ).splitlines()
    rows = [line.split() for line in objects]
    reachable_set = set(reachable)
    require(
        all(
            len(row) == 3
            and row[0] in reachable_set
            and row[1] in {b"blob", b"tree", b"commit", b"tag"}
            and row[2].isdigit()
            for row in rows
        ),
        "OFFLINE_OBJECT_CLOSURE",
    )
    require(
        len(rows) == len(reachable) and {r[0] for r in rows} == set(reachable),
        "OFFLINE_OBJECT_CLOSURE",
    )
    commits = {r[0] for r in rows if r[1] == b"commit"}
    parents = run(["rev-list", "--parents", head], "parents", 2 * 1024**2).splitlines()
    graph = [line.split() for line in parents]
    require(
        0 < len(graph) <= 20000
        and len(graph) == len(commits)
        and {r[0] for r in graph} == commits
        and all(len(r[1:]) == len(set(r[1:])) and all(p in commits for p in r[1:]) for r in graph),
        "OFFLINE_PARENT_CLOSURE",
    )
    roots = sorted(r[0].decode() for r in graph if len(r) == 1)
    types = dict(Counter(r[1].decode() for r in rows))
    require(
        roots
        and len(graph) == expected.get("reachable_commit_count")
        and len(rows) == expected.get("reachable_object_count")
        and types == expected.get("object_type_counts")
        and roots == expected.get("root_commits"),
        "OFFLINE_EXTERNAL_COUNT_MISMATCH",
    )
    require(
        object_hash("commit", run(["cat-file", "commit", head], "head-object", 512 * 1024)) == head,
        "OFFLINE_OBJECT_IDENTITY",
    )
    for path in sorted((bare / "objects/pack").glob("*.idx")):
        run(["verify-pack", str(path)], "pack", seconds=120)
    licenses = []
    listing = run(["ls-tree", "-z", head, "--", *LICENSES], "license-tree")
    for item in filter(None, listing.split(b"\0")):
        metadata, name = item.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        name = name.decode("ascii")
        require(
            name in LICENSES and mode in {b"100644", b"100755"} and kind == b"blob",
            "OFFLINE_LICENSE_INVALID",
        )
        body = run(["cat-file", "blob", oid.decode()], "license", 512 * 1024)
        expected_file = raw / "licenses" / repository.split("/")[1] / name
        require(
            object_hash("blob", body) == oid.decode()
            and fingerprint(expected_file, budget.tick, 512 * 1024)
            == {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()},
            "OFFLINE_LICENSE_DRIFT",
        )
        licenses.append(
            {
                "path": name,
                "blob": oid.decode(),
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    require(
        any(x["path"].startswith("LICENSE") for x in licenses)
        and any(x["path"].startswith("NOTICE") for x in licenses),
        "OFFLINE_LICENSE_MISSING",
    )
    return {
        "repository": repository,
        "head": head,
        "commits": len(graph),
        "objects": len(rows),
        "object_types": types,
        "roots": roots,
        "parent_edges": sum(len(r) - 1 for r in graph),
        "objects_digest": hashlib.sha256(b"\n".join(sorted(objects))).hexdigest(),
        "parents_digest": hashlib.sha256(b"\n".join(sorted(parents))).hexdigest(),
        "licenses": licenses,
        "source_inventory": inventory(bare, budget.tick),
        "external_acquired_at": expected["acquired_utc"],
    }


def safe_code(error):
    code = (
        str(error)
        if isinstance(error, (SemanticError, assets.AssetError))
        else getattr(error, "code", "OFFLINE_ADMISSION_FAILED")
    )
    if not isinstance(code, str) or re.fullmatch("[A-Z][A-Z0-9_]{1,95}", code) is None:
        code = "OFFLINE_ADMISSION_FAILED"
    return code


def failure(attempt, error, budget):
    code = safe_code(error)
    assets.write_json(
        attempt / "failure.json",
        {
            "success": False,
            "code": code,
            "commands": budget.commands if budget is not None else [],
            "original_material_retained": True,
            "no_success_published": True,
        },
    )


def publish_directory(source, target):
    """Atomic no-replace publication, including a concurrently created empty directory."""
    source, target = assets.safe_path(source), assets.safe_path(target)
    if os.name == "nt":
        os.rename(filesystem_path(source), filesystem_path(target))
        return
    # POSIX rename can overwrite an empty directory. Require the Linux no-replace primitive.
    library = ctypes.CDLL(None, use_errno=True)
    operation = getattr(library, "renameat2", None)
    require(operation is not None, "OFFLINE_ATOMIC_PUBLICATION_UNSUPPORTED")
    operation.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    if operation(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def admit(archive, output, check=native_runtime.checkpoint):
    archive, output = assets.safe_path(archive), assets.safe_path(output)
    require(
        not output.is_relative_to(archive) and not archive.is_relative_to(output),
        "OFFLINE_INPUT_OUTPUT_OVERLAP",
    )
    output.mkdir(parents=True, exist_ok=True)
    target = assets.safe_path(output / ARCHIVE_SHA256)
    require(not target.exists(), "OFFLINE_TARGET_EXISTS")
    lock = output / (".publish-" + ARCHIVE_SHA256)
    try:
        lock.mkdir()
    except FileExistsError:
        raise SemanticError("OFFLINE_PUBLICATION_BUSY") from None
    attempt, budget = output / (".offline-" + uuid4().hex), None
    try:
        attempt.mkdir()
    except BaseException:
        lock.rmdir()
        raise
    try:
        initial_bindings = bindings()
        budget = Budget(attempt, check)
        archive_record, members, manifest = inspect_archive(archive, budget, attempt / "extracted")
        raw, sources = attempt / "extracted", []
        for expected in manifest["sources"]:
            short = expected["name"].split("/")[1]
            bare = attempt / "sources" / short / "source.git"
            clean_copy(
                raw / "repositories" / (short + ".git"), bare, expected["pinned_commit"], budget
            )
            facts = native_facts(bare, attempt / "checks" / short, raw, expected, budget)
            replay = attempt / "replay" / short / "source.git"
            replay.parent.mkdir(parents=True)
            shutil.copytree(
                filesystem_path(bare), filesystem_path(replay), copy_function=shutil.copyfile
            )
            repeated = native_facts(
                replay, attempt / "replay-checks" / short, raw, expected, budget
            )
            require(facts == repeated, "OFFLINE_REPLAY_DRIFT")
            sources.append(facts)
        require(inventory(raw, budget.tick) == members, "OFFLINE_INPUT_DRIFT")
        require(
            fingerprint(archive, budget.tick, MAX_ARCHIVE_BYTES) == archive_record,
            "OFFLINE_INPUT_DRIFT",
        )
        require(bindings() == initial_bindings, "OFFLINE_EXECUTION_DRIFT")
        files = inventory(attempt, budget.tick)
        budget.final()
        require(bindings() == initial_bindings, "OFFLINE_EXECUTION_DRIFT")
        require(
            fingerprint(archive, budget.tick, MAX_ARCHIVE_BYTES) == archive_record,
            "OFFLINE_INPUT_DRIFT",
        )
        receipt = assets.sealed(
            {
                "version": VERSION,
                "archive": archive_record,
                "source_bindings": initial_bindings,
                "git_executable": budget.git_identity,
                "verified_at": datetime.now(UTC).isoformat(),
                "external_provenance": manifest,
                "archive_members": members,
                "sources": sources,
                "files": files,
                "commands": budget.commands,
                "owned_process_cleanup_verified": True,
                "peak_sampled_git_tree_rss": budget.peak,
                "copied_root_replay_exact": True,
                "online_acquisition_certified": False,
                "training_ready": False,
                "candidate_release": False,
                "upstream_code_executed": False,
            }
        )
        # Publication is last. Retain a failed attempt without a success receipt on any prior error.
        require(not target.exists(), "OFFLINE_TARGET_EXISTS")
        budget.tick()
        assets.write_json(attempt / "receipt.json", receipt)
        try:
            budget.tick()
            require(not target.exists(), "OFFLINE_TARGET_EXISTS")
            publish_directory(attempt, target)
        except BaseException:
            (attempt / "receipt.json").unlink()
            raise
        return target
    except BaseException as error:
        failure(attempt, error, budget)
        raise
    finally:
        lock.rmdir()


def verify(archive, directory, check=native_runtime.checkpoint):
    archive, directory = assets.safe_path(archive), assets.safe_path(directory)
    require(directory.name == ARCHIVE_SHA256, "OFFLINE_RECEIPT_IDENTITY")
    attempt = directory.parent / (".verify-" + uuid4().hex)
    attempt.mkdir()
    budget = None
    try:
        initial_bindings = bindings()
        budget = Budget(attempt, check)
        receipt = assets.read_json(directory / "receipt.json", budget.tick, maximum=4 * 1024**2)
        assets.verify_seal(receipt)
        require(
            receipt.get("version") == VERSION
            and receipt.get("source_bindings") == initial_bindings
            and receipt.get("git_executable") == budget.git_identity,
            "OFFLINE_EXECUTION_DRIFT",
        )
        require(
            all(
                receipt.get(n) is False
                for n in (
                    "training_ready",
                    "candidate_release",
                    "upstream_code_executed",
                    "online_acquisition_certified",
                )
            ),
            "OFFLINE_QUALIFICATION_DRIFT",
        )
        actual, members, manifest = inspect_archive(archive, budget)
        require(
            receipt.get("archive") == actual
            and receipt.get("archive_members") == members
            and receipt.get("external_provenance") == manifest
            and inventory(directory / "extracted", budget.tick) == members,
            "OFFLINE_INPUT_DRIFT",
        )
        require(
            inventory(directory, budget.tick, exclude=("receipt.json",)) == receipt.get("files"),
            "OFFLINE_RECEIPT_DRIFT",
        )
        derived = []
        for source in manifest["sources"]:
            short = source["name"].split("/")[1]
            bare = directory / "sources" / short / "source.git"
            facts = native_facts(bare, attempt / short, directory / "extracted", source, budget)
            # Compare pack bytes with the approved archive, rather than trusting a supplied seal.
            for name, row in facts["source_inventory"].items():
                if name.startswith("objects/pack/"):
                    require(
                        members.get(f"repositories/{short}.git/{name}") == row,
                        "OFFLINE_INPUT_DRIFT",
                    )
            derived.append(facts)
        require(derived == receipt.get("sources"), "OFFLINE_NATIVE_FACT_DRIFT")
        require(bindings() == initial_bindings, "OFFLINE_EXECUTION_DRIFT")
        require(
            inventory(directory, budget.tick, exclude=("receipt.json",)) == receipt["files"],
            "OFFLINE_RECEIPT_DRIFT",
        )
        budget.final()
        require(bindings() == initial_bindings, "OFFLINE_EXECUTION_DRIFT")
        require(
            fingerprint(archive, budget.tick, MAX_ARCHIVE_BYTES) == actual,
            "OFFLINE_INPUT_DRIFT",
        )
        assets.write_json(
            attempt / "verification.json",
            assets.sealed(
                {
                    "receipt_hash": receipt["content_hash"],
                    "commands": budget.commands,
                    "source_bindings": initial_bindings,
                    "independently_rederived": True,
                    "owned_process_cleanup_verified": True,
                    "verified_at": datetime.now(UTC).isoformat(),
                }
            ),
        )
        return receipt
    except BaseException as error:
        failure(attempt, error, budget)
        raise
