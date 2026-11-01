"""Bounded data-only acquisition for controlled supplemental Git inputs."""

import hashlib
import json
import os
import shutil
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psutil

from packages.mining import public_assets as assets
from packages.repositories.clone import CloneExecutor
from packages.repositories.safety import URLPolicy, canonicalize
from packages.repositories.storage import directory_bytes
from packages.repositories.transport import Tunnel
from packages.semantic import artifacts, native_runtime, supplement_policy
from packages.semantic.admission_git import native_environment, object_hash
from packages.semantic.policy import SemanticError

BINDINGS = (
    "packages/semantic/supplement_policy.py",
    "packages/semantic/supplement_source.py",
    "packages/semantic/native_runtime.py",
    "packages/semantic/admission_git.py",
    "packages/semantic/artifacts.py",
    "packages/mining/public_assets.py",
    "packages/repositories/clone.py",
    "packages/repositories/safety.py",
    "packages/repositories/storage.py",
    "packages/repositories/transport.py",
)


def require(condition, code):
    if not condition:
        raise SemanticError(code)


def validate_layout(bare, check):
    """Layout refusal only; actual object/HEAD verification is separate."""
    check()
    bare = assets.safe_path(Path(bare))
    for name in (
        "shallow",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "refs/replace",
    ):
        path = bare / name
        require(not path.exists() and not path.is_symlink(), "SUPPLEMENT_UNSAFE_GIT_LAYOUT")
    count = 0
    for current, dirs, files in os.walk(bare, followlinks=False):
        check()
        for name in dirs + files:
            path = assets.safe_path(Path(current) / name)
            mode = path.lstat()
            require(not path.is_symlink(), "SUPPLEMENT_UNSAFE_GIT_LAYOUT")
            require(
                not name.endswith(".promisor")
                and (
                    stat.S_ISDIR(mode.st_mode) or stat.S_ISREG(mode.st_mode) and mode.st_nlink == 1
                ),
                "SUPPLEMENT_UNSAFE_GIT_LAYOUT",
            )
            count += 1
            require(count <= 8192, "SUPPLEMENT_SOURCE_FILE_LIMIT")


def acquire(repository, head, output, check=native_runtime.checkpoint):
    approved = supplement_policy.approved_source(repository, head)
    limits = supplement_policy.acquisition_limits()
    output = assets.safe_path(Path(output))
    target = assets.safe_path(output / (repository.split("/")[1] + "-" + head))
    require(not target.exists(), "SUPPLEMENT_SOURCE_EXISTS")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".acquire-" + uuid4().hex)
    attempt.mkdir()
    bare = attempt / "source.git"
    git = shutil.which("git")
    require(git is not None, "SUPPLEMENT_GIT_MISSING")
    binding = artifacts.sources(BINDINGS)
    started = time.monotonic()
    deadline = started + limits["seconds"]
    peak_rss = 0
    commands = []
    published = False

    def tick():
        check()
        require(time.monotonic() < deadline, "SUPPLEMENT_ACQUISITION_TIMEOUT")
        require(shutil.disk_usage(output).free >= 2.5 * 1024**3, "SUPPLEMENT_LOW_STORAGE")
        require(directory_bytes(attempt) <= limits["git_bytes"], "SUPPLEMENT_SOURCE_BYTE_LIMIT")
        require(directory_bytes(output) <= 1024**3, "SUPPLEMENT_OUTPUT_BYTE_LIMIT")

    def observe(pid, created, descendants):
        nonlocal peak_rss
        rss = 0
        for identity, expected in ({pid: created} | descendants).items():
            if expected is None:
                continue
            try:
                process = psutil.Process(identity)
                if process.create_time() == expected:
                    rss += process.memory_info().rss
            except psutil.NoSuchProcess:
                continue
        peak_rss = max(peak_rss, rss)
        require(rss <= limits["rss_bytes"], "SUPPLEMENT_PROCESS_MEMORY")

    def command(args, environment, name, *, seconds=30, limit=65536):
        def capture(stdout, stderr, truncated):
            row = {
                "stdout_bytes": len(stdout),
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_bytes": len(stderr),
                "stderr_bounded": stderr[:16384].decode("utf-8", "replace"),
                "truncated": list(truncated),
            }
            with (attempt / (name + ".json")).open("x", encoding="utf-8") as stream:
                json.dump(row, stream, sort_keys=True)

        tick()
        result = native_runtime.run(
            [git, *args],
            directory=attempt,
            environment=environment,
            check=tick,
            deadline=min(deadline, time.monotonic() + seconds),
            limit=limit,
            observe=observe,
            capture=capture,
        )
        require(result.cleanup_complete, "SUPPLEMENT_PROCESS_CLEANUP")
        commands.append({"phase": name, "seconds": result.seconds, "cleanup_complete": True})
        return result.data

    try:
        target_url = canonicalize(approved["url"])
        policy = URLPolicy()
        policy.addresses(target_url)
        executor = object.__new__(CloneExecutor)
        executor.cafile = None
        with Tunnel(target_url, policy, limits["transfer_bytes"]) as tunnel:
            environment, generated_template = executor.environment(attempt, tunnel)
            environment.update(GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1")
            command(
                ["init", "--bare", "--template=" + str(generated_template), str(bare)],
                environment,
                "init",
                seconds=15,
            )
            command(
                [
                    "-C",
                    str(bare),
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-recurse-submodules",
                    approved["url"],
                    head + ":refs/heads/pinned",
                ],
                environment,
                "fetch",
                seconds=limits["seconds"],
            )
            transferred = tunnel.bytes
        readonly = native_environment(attempt, bare)
        require(
            command(
                ["-C", str(bare), "rev-parse", "--is-bare-repository"], readonly, "bare"
            ).strip()
            == b"true",
            "SUPPLEMENT_STORAGE_INVALID",
        )
        require(
            command(
                ["-C", str(bare), "rev-parse", "--is-shallow-repository"], readonly, "shallow"
            ).strip()
            == b"false",
            "SUPPLEMENT_UNSAFE_GIT_LAYOUT",
        )
        actual = (
            command(
                ["-C", str(bare), "rev-parse", "refs/heads/pinned^{commit}"],
                readonly,
                "fixed-head",
            )
            .strip()
            .decode("ascii")
        )
        require(actual == head, "SUPPLEMENT_HEAD_DRIFT")
        command(
            ["-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/pinned"],
            readonly,
            "head-reference",
        )
        validate_layout(bare, check)
        raw = command(["-C", str(bare), "cat-file", "commit", head], readonly, "head-object")
        require(object_hash("commit", raw) == head, "SUPPLEMENT_OBJECT_DRIFT")
        licenses = []
        license_dir = attempt / "licenses"
        license_dir.mkdir()
        allowed = ("LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "NOTICE.txt", "NOTICE.md")
        listing = command(
            ["-C", str(bare), "ls-tree", "-z", head, "--", *allowed],
            readonly,
            "license-tree",
        )
        for index, item in enumerate(filter(None, listing.split(b"\0"))):
            metadata, raw_name = item.split(b"\t", 1)
            mode, kind, identity = metadata.split()
            name = raw_name.decode("ascii")
            require(
                name in allowed and mode in {b"100644", b"100755"} and kind == b"blob",
                "SUPPLEMENT_LICENSE_INVALID",
            )
            body = command(
                ["-C", str(bare), "cat-file", "blob", identity.decode("ascii")],
                readonly,
                "license-" + str(index),
                limit=512 * 1024,
            )
            require(
                object_hash("blob", body) == identity.decode("ascii"), "SUPPLEMENT_LICENSE_DRIFT"
            )
            (license_dir / name).write_bytes(body)
            licenses.append(
                {
                    "path": name,
                    "git_blob": identity.decode("ascii"),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                }
            )
        require(
            any(x["path"].startswith("LICENSE") for x in licenses), "SUPPLEMENT_LICENSE_MISSING"
        )
        inventory = {}
        for path in sorted((bare / "objects").rglob("*")):
            tick()
            if path.is_file():
                path = assets.safe_path(path)
                inventory[path.relative_to(attempt).as_posix()] = assets.file_digest(
                    path, check, maximum=limits["git_bytes"]
                )
        require(inventory and len(inventory) <= 4096, "SUPPLEMENT_OBJECT_INVENTORY")
        metadata_inventory = {
            name: assets.file_digest(assets.safe_path(bare / name), check, maximum=65536)
            for name in ("config", "HEAD", "refs/heads/pinned")
        }
        require(artifacts.sources(BINDINGS) == binding, "SUPPLEMENT_EXECUTION_DRIFT")
        tick()
        receipt = assets.sealed(
            {
                "version": supplement_policy.VERSION + "-acquisition",
                **approved,
                "acquired_at": datetime.now(UTC).isoformat(),
                "git_bytes": directory_bytes(bare),
                "transferred_bytes": transferred,
                "object_inventory": inventory,
                "licenses": licenses,
                "git_metadata_inventory": metadata_inventory,
                "source_bindings": binding,
                "commands": commands,
                "peak_sampled_git_tree_rss": peak_rss,
                "seconds": time.monotonic() - started,
                "checkout": False,
                "upstream_code_executed": False,
                "main_reachable_history_only": True,
                "all_branch_history_certified": False,
                "license_scope": (
                    "Original fixed source documents; "
                    "no blanket historical/model redistribution approval."
                ),
                "training_ready": False,
                "candidate_release": False,
            }
        )
        assets.write_json(attempt / "acquisition.json", receipt)
        attempt.rename(target)
        published = True
        return target
    finally:
        if not published and attempt.exists():
            assets.write_json(
                attempt / "failed-attempt.json",
                {
                    "success": False,
                    "source": approved,
                    "commands_completed": commands,
                    "elapsed_seconds": time.monotonic() - started,
                    "scope": "Owned failed attempt retained; no successful source publication.",
                },
            )
