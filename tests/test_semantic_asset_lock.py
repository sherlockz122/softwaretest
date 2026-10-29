"""Real child processes verify mutual exclusion and OS release after forced exit."""

import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil
import pytest

from packages.mining import public_assets as assets
from packages.semantic import asset_catalog as catalog
from packages.semantic import asset_lock as locks
from packages.semantic.asset_reader import ConsumptionError
from tests.semantic_asset_support import source

HOLDER = """
import sys,time
from pathlib import Path
from packages.semantic.asset_lock import publication
with publication(Path(sys.argv[1])):
 Path(sys.argv[2]).write_text('locked',encoding='ascii')
 time.sleep(30)
"""
CONTENDER = """
import sys
from pathlib import Path
from packages.semantic.asset_lock import publication
from packages.semantic.asset_reader import ConsumptionError
try:
 with publication(Path(sys.argv[1])):print('acquired')
except ConsumptionError as exc:
 print(exc.code);sys.exit(3)
"""


@pytest.fixture
def directory():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="semantic-lock-") as temp:
        yield Path(temp)


def holder(directory):
    ready = directory / ".ready.txt"
    process = subprocess.Popen([sys.executable, "-c", HOLDER, str(directory), str(ready)])
    stop = time.monotonic() + 10
    while not ready.exists():
        assert process.poll() is None
        assert time.monotonic() < stop
        time.sleep(0.02)
    return process


def kill_owned(process):
    if process.poll() is None:
        children = psutil.Process(process.pid).children(recursive=True)
        for child in reversed(children):
            child.kill()
        process.kill()
        process.wait(timeout=10)
        psutil.wait_procs(children, timeout=10)


def contend(directory):
    return subprocess.run(
        [sys.executable, "-c", CONTENDER, str(directory)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_independent_contender_rejected_then_killed_owner_released(directory):
    sentinel = directory / "old-asset.txt"
    sentinel.write_bytes(b"old immutable bytes")
    process = holder(directory)
    lock = directory / locks.NAME
    inode = lock.stat().st_ino
    try:
        result = contend(directory)
        assert result.returncode == 3 and result.stdout.strip() == "SEMANTIC_ASSET_INSTALL_BUSY"
        owner = locks.owner(directory)
        assert owner["state"] == "held" and owner["version"] == 2 and owner["pid"] > 0
        kill_owned(process)
        result = contend(directory)
        assert result.returncode == 0 and result.stdout.strip() == "acquired", result.stderr
        assert lock.stat().st_ino == inode
        assert locks.owner(directory)["state"] == "released"
        assert sentinel.read_bytes() == b"old immutable bytes"
    finally:
        kill_owned(process)


def test_release_and_exception_keep_same_persistent_lock_path(directory):
    with locks.publication(directory):
        inode = (directory / locks.NAME).stat().st_ino
    with pytest.raises(RuntimeError, match="injected"):
        with locks.publication(directory):
            raise RuntimeError("injected")
    with locks.publication(directory):
        assert (directory / locks.NAME).stat().st_ino == inode


@pytest.mark.parametrize("raw", [b"x", b"x" * 4097, b'{"version":2}', b"null", b"[]"])
def test_unrecognized_or_oversized_lock_fails_closed_without_deletion(directory, raw):
    lock = directory / locks.NAME
    lock.write_bytes(raw)
    with pytest.raises(ConsumptionError, match="SEMANTIC_ASSET_LOCK_INVALID"):
        with locks.publication(directory):
            pytest.fail("unsafe lock admitted")
    assert lock.read_bytes() == raw


def test_legacy_lock_not_deleted_or_guessed_stale(directory):
    legacy = directory / ".publication-lock"
    legacy.write_bytes(b"unknown stage30 owner")
    with pytest.raises(ConsumptionError, match="SEMANTIC_ASSET_INSTALL_BUSY"):
        with locks.publication(directory):
            pytest.fail("legacy lock bypassed")
    assert legacy.read_bytes() == b"unknown stage30 owner"
    assert not (directory / locks.NAME).exists()


def test_empty_interrupted_creation_is_safe_only_after_os_acquisition(directory):
    (directory / locks.NAME).write_bytes(b"")
    with locks.publication(directory):
        assert locks.owner(directory)["state"] == "held"


def test_partial_v2_diagnostic_reinitialized_only_after_os_lock_is_owned(directory):
    lock = directory / locks.NAME
    lock.write_bytes(b'\0{"version":2,"pid":')
    with locks.publication(directory):
        assert locks.owner(directory)["state"] == "held"
    assert locks.owner(directory)["state"] == "released"


def test_hardlinked_lock_is_rejected_preserving_original(directory):
    original = directory / "original.txt"
    original.write_bytes(b"original")
    os.link(original, directory / locks.NAME)
    with pytest.raises(ConsumptionError, match="SEMANTIC_ASSET_LOCK_INVALID"):
        with locks.publication(directory):
            pytest.fail("hard link admitted")
    assert original.read_bytes() == b"original"


def test_actual_installer_obeys_os_lock_then_publishes_after_holder_kill(directory):
    producer = directory / "producer"
    producer.mkdir()
    inputs, manifest = source(producer)
    store = directory / "catalog"
    store.mkdir()
    process = holder(store)
    try:
        with pytest.raises(ConsumptionError, match="SEMANTIC_ASSET_INSTALL_BUSY"):
            catalog.install(inputs, manifest["content_hash"], store=store)
        assert catalog.catalog_ids(store) == []
        kill_owned(process)
        installed = catalog.install(inputs, manifest["content_hash"], store=store)
        assert catalog.verify(installed)[0]["accepted"] == 660
        files = {p.name: p.read_bytes() for p in installed.iterdir()}
        assert catalog.install(inputs, manifest["content_hash"], store=store) == installed
        assert files == {p.name: p.read_bytes() for p in installed.iterdir()}
        assert (store / locks.NAME).exists()
    finally:
        kill_owned(process)
