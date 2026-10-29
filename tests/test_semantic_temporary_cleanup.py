"""Independent stream-cleanup failures, original errors and real open-file semantics."""

import errno
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import public_assets as assets
from packages.semantic import admission_git as native
from packages.semantic import native_runtime
from packages.semantic.policy import SemanticError
from tests.repository_support import git


@pytest.fixture
def reader():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="stream-cleanup-") as directory:
        work = Path(directory)
        assert work.resolve().is_relative_to(assets.ROOT.resolve())
        bare = work / "repo.git"
        git("init", "--bare", str(bare))
        tree = git("--git-dir", str(bare), "mktree", data=b"").strip().decode("ascii")
        environment = os.environ | {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        commit = (
            git("--git-dir", str(bare), "commit-tree", tree, "-m", "fixture", env=environment)
            .strip()
            .decode("ascii")
        )
        git("--git-dir", str(bare), "update-ref", "refs/heads/main", commit)
        home = work / "home"
        home.mkdir()
        yield native.NativeReader(
            bare, home, native.native_environment(home, bare), lambda: None, time.monotonic() + 30
        )


def sharing_error(code=32):
    error = PermissionError(errno.EACCES, "fixture sharing violation")
    error.winerror = code
    return error


@pytest.mark.parametrize("stream", ["read.out", "read.err"])
@pytest.mark.parametrize("code", [32, 33])
def test_transient_sharing_preserves_real_output_limit_and_cleans_both(
    reader, monkeypatch, stream, code
):
    unlink = Path.unlink
    target = reader.home / stream
    calls = []

    def injected(path, *args, **kwargs):
        if path == target:
            calls.append(path)
            if len(calls) <= 2:
                raise sharing_error(code)
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", injected)
    with pytest.raises(SemanticError, match="ADMISSION_GIT_OUTPUT_LIMIT"):
        reader.command(["rev-list", "--all"], limit=1)
    assert len(calls) == 3
    assert not any((reader.home / name).exists() for name in ["read.out", "read.err"])


def test_transient_cleanup_never_turns_deadline_into_success(reader, monkeypatch):
    unlink = Path.unlink
    calls = []

    def injected(path, *args, **kwargs):
        if path == reader.home / "read.err":
            calls.append(path)
            if len(calls) == 1:
                raise sharing_error()
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", injected)
    reader.deadline = 0
    with pytest.raises(SemanticError, match="ADMISSION_GIT_DEADLINE"):
        reader.command(["rev-parse", "HEAD"])
    assert len(calls) == 2 and not (reader.home / "read.err").exists()


def test_normal_result_is_exact_after_both_streams_temporarily_locked(reader, monkeypatch):
    expected = git("--git-dir", str(reader.bare), "rev-list", "--all")
    unlink = Path.unlink
    attempts = {reader.home / name: 0 for name in ["read.out", "read.err"]}

    def injected(path, *args, **kwargs):
        if path in attempts:
            attempts[path] += 1
            if attempts[path] <= 2:
                raise sharing_error(33)
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", injected)
    assert reader.command(["rev-list", "--all"]) == expected
    assert set(attempts.values()) == {3}
    assert not any(path.exists() for path in attempts)


def test_persistent_sharing_is_bounded_retains_file_and_cleans_other_stream(reader, monkeypatch):
    unlink = Path.unlink
    calls = []

    def injected(path, *args, **kwargs):
        if path == reader.home / "read.out":
            calls.append(path)
            raise sharing_error()
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", injected)
    started = time.monotonic()
    with pytest.raises(PermissionError) as captured:
        reader.command(["rev-list", "--all"], limit=1)
    assert captured.value.winerror == 32
    assert "ADMISSION_GIT_OUTPUT_LIMIT" in str(captured.value.__context__)
    assert 0.4 <= time.monotonic() - started < 3
    assert len(calls) >= 2 and (reader.home / "read.out").is_file()
    assert not (reader.home / "read.err").exists()


@pytest.mark.parametrize("winerror", [None, 5])
def test_nonsharing_denial_is_not_retried_or_ignored(reader, monkeypatch, winerror):
    unlink = Path.unlink
    calls = []

    def injected(path, *args, **kwargs):
        if path == reader.home / "read.out":
            calls.append(path)
            error = PermissionError(errno.EACCES, "fixture access denial")
            if winerror is not None:
                error.winerror = winerror
            raise error
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", injected)
    with pytest.raises(PermissionError) as captured:
        reader.command(["rev-list", "--all"], limit=1)
    assert "ADMISSION_GIT_OUTPUT_LIMIT" in str(captured.value.__context__)
    assert len(calls) == 1 and (reader.home / "read.out").is_file()
    assert not (reader.home / "read.err").exists()


@pytest.mark.parametrize("persistent", [False, True])
def test_real_subprocess_held_file_has_explicit_platform_semantics(reader, persistent):
    state = {}
    ready = reader.home / "holder-ready"
    script = (
        "import sys,time;from pathlib import Path\n"
        "with open(sys.argv[1],'rb') as stream:\n"
        " Path(sys.argv[2]).write_text('ready',encoding='ascii')\n"
        " time.sleep(float(sys.argv[3]))\n"
    )

    def tick():
        if state:
            return
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(reader.home / "read.out"),
                str(ready),
                "5" if persistent else "0.15",
            ],
            cwd=reader.home,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        state.update(process=process, owned=native_runtime._Owned(process))
        deadline = time.monotonic() + 10
        while not ready.exists():
            state["owned"].sample()
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        state["owned"].sample()

    reader.tick = tick
    try:
        if os.name == "nt" and persistent:
            with pytest.raises(PermissionError) as captured:
                reader.command(["rev-list", "--all"], limit=1)
            assert captured.value.winerror == 32
            assert "ADMISSION_GIT_OUTPUT_LIMIT" in str(captured.value.__context__)
            assert state["process"].poll() is None
            assert (reader.home / "read.out").is_file()
            assert not (reader.home / "read.err").exists()
        else:
            with pytest.raises(SemanticError, match="ADMISSION_GIT_OUTPUT_LIMIT"):
                reader.command(["rev-list", "--all"], limit=1)
            assert not any((reader.home / name).exists() for name in ["read.out", "read.err"])
    finally:
        if state:
            state["owned"].close()
            state["process"].stderr.close()
