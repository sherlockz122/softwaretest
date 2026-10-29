"""Minimal offline Git reader for the isolated tokenizer environment; no ORM/miner imports."""

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from packages.mining import public_assets as assets
from packages.semantic.policy import SemanticError


def sha_valid(value):
    if not isinstance(value, bytes) or re.fullmatch(b"[0-9a-f]{40}", value) is None:
        raise SemanticError("ADMISSION_SHA_INVALID")
    return value.decode("ascii")


def object_hash(kind, body):
    return hashlib.sha1(f"{kind} {len(body)}\0".encode() + body).hexdigest()


def text(body):
    if b"\0" in body:
        raise SemanticError("ADMISSION_BINARY")
    try:
        value = body.decode("utf-8", "strict")
    except UnicodeError:
        raise SemanticError("ADMISSION_ENCODING") from None
    if len(value.splitlines()) > 5000:
        raise SemanticError("ADMISSION_LINE_LIMIT")
    return value


def native_environment(home, bare):
    home, bare = assets.safe_path(Path(home)), assets.safe_path(Path(bare))
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
        "safe.directory": str(bare),
    }
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for i, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return env


def _unlink_closed_stream(path):
    """Bounded Windows sharing cleanup after the child and our stream have closed."""
    deadline = time.monotonic() + 0.5
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if getattr(error, "winerror", None) not in {32, 33} or remaining <= 0:
                raise
            time.sleep(min(0.02, remaining))


class NativeReader:
    def __init__(self, bare, home, environment, tick, deadline):
        self.bare, self.home = assets.safe_path(Path(bare)), assets.safe_path(Path(home))
        self.environment, self.tick, self.deadline = environment, tick, deadline
        self.git = shutil.which("git")
        if self.git is None:
            raise SemanticError("ADMISSION_GIT_MISSING")

    def command(self, arguments, limit=65536):
        if not arguments or arguments[0] not in {"rev-parse", "rev-list", "cat-file"}:
            raise SemanticError("ADMISSION_GIT_COMMAND_FORBIDDEN")
        if type(limit) is not int or not 0 < limit <= 4 * 1024**2:
            raise SemanticError("ADMISSION_GIT_OUTPUT_LIMIT")
        output, errors = self.home / "read.out", self.home / "read.err"
        process = None
        try:
            with output.open("xb") as stdout, errors.open("xb") as stderr:
                # Inherit the supervised parent's group so a Linux group kill also stops Git.
                process = subprocess.Popen(
                    [self.git, "--literal-pathspecs", "-C", str(self.bare), *arguments],
                    cwd=self.home,
                    env=self.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                )
                while True:
                    self.tick()
                    if time.monotonic() >= self.deadline:
                        raise SemanticError("ADMISSION_GIT_DEADLINE")
                    if output.stat().st_size > limit or errors.stat().st_size > 65536:
                        raise SemanticError("ADMISSION_GIT_OUTPUT_LIMIT")
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
            # The child may flush its final burst between stat() and poll().
            # Recheck after exit/closed handles before reading any result into memory.
            self.tick()
            if time.monotonic() >= self.deadline:
                raise SemanticError("ADMISSION_GIT_DEADLINE")
            if output.stat().st_size > limit or errors.stat().st_size > 65536:
                raise SemanticError("ADMISSION_GIT_OUTPUT_LIMIT")
            if process.returncode:
                raise SemanticError("ADMISSION_GIT_READ_FAILED")
            return output.read_bytes()
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            cleanup_errors = []
            for path in (output, errors):
                try:
                    _unlink_closed_stream(path)
                except OSError as error:
                    cleanup_errors.append(error)
            if cleanup_errors:
                raise cleanup_errors[0]
