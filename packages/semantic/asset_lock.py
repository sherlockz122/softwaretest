"""Local persistent OS lock; never unlink an inode that another writer could hold."""

import json
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from packages.semantic import asset_reader as reader

NAME = ".publication-lock-v2"
MAXIMUM = 4096


def _identity(path, stream):
    opened, current = os.fstat(stream.fileno()), reader.safe(path).stat()
    reader.require(
        stat.S_ISREG(opened.st_mode)
        and opened.st_nlink == 1
        and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
        and 0 <= opened.st_size <= MAXIMUM,
        "SEMANTIC_ASSET_LOCK_INVALID",
    )


def _metadata(raw):
    if not raw:  # Creation can be interrupted before the first owner record is written.
        return
    try:
        value = json.loads(raw, object_pairs_hook=reader.pairs)
        reader.require(
            isinstance(value, dict)
            and set(value) == {"version", "pid", "started_at", "nonce", "state"}
            and type(value["version"]) is int
            and value["version"] == 2
            and type(value["pid"]) is int
            and value["pid"] > 0
            and isinstance(value["nonce"], str)
            and len(value["nonce"]) == 32
            and all(c in "0123456789abcdef" for c in value["nonce"])
            and value["state"] in {"held", "released"}
            and isinstance(value["started_at"], str)
            and datetime.fromisoformat(value["started_at"]).utcoffset().total_seconds() == 0,
            "SEMANTIC_ASSET_LOCK_INVALID",
        )
        return value
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError) as exc:
        raise reader.ConsumptionError("SEMANTIC_ASSET_LOCK_INVALID") from exc


def _write(stream, record):
    raw = b"\0" + (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    reader.require(len(raw) <= MAXIMUM, "SEMANTIC_ASSET_LOCK_INVALID")
    stream.seek(0)
    stream.write(raw)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def owner(store):
    """Bounded diagnostic snapshot outside the locked byte; never a liveness test."""
    path = reader.safe(reader.safe(store) / NAME)
    with path.open("rb") as stream:
        _identity(path, stream)
        stream.seek(1)
        return _metadata(stream.read(MAXIMUM))


@contextmanager
def publication(store):
    """Nonblocking, local-filesystem lock, requiring all legacy writers stopped."""
    store = reader.safe(store)
    legacy = store / ".publication-lock"
    reader.require(not legacy.exists() and not legacy.is_symlink(), "SEMANTIC_ASSET_INSTALL_BUSY")
    path = reader.safe(store / NAME)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        _identity(path, stream)
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise reader.ConsumptionError("SEMANTIC_ASSET_INSTALL_BUSY") from exc
        except OSError as exc:
            if exc.errno in {11, 13, 36}:
                raise reader.ConsumptionError("SEMANTIC_ASSET_INSTALL_BUSY") from exc
            raise
        try:
            _identity(path, stream)
            reader.require(
                not legacy.exists() and not legacy.is_symlink(), "SEMANTIC_ASSET_INSTALL_BUSY"
            )
            stream.seek(0)
            raw = stream.read(MAXIMUM + 1)
            reader.require(not raw or raw[:1] == b"\0", "SEMANTIC_ASSET_LOCK_INVALID")
            # A killed writer can leave a partial diagnostic record. The OS lock,
            # regular-file identity, bound and v2 marker authorize reinitialization;
            # owner text never decides liveness or permits lock-path deletion.
            record = {
                "version": 2,
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                "nonce": uuid4().hex,
                "state": "held",
            }
            _write(stream, record)
            _identity(path, stream)
            try:
                yield
            finally:
                _identity(path, stream)
                _write(stream, record | {"state": "released"})
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
