"""Owned real child trees and terminal races; no services or upstream code."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil
import pytest

from packages.mining import public_assets as assets
from packages.repositories.safety import RepositoryError
from packages.semantic import native_runtime as runtime
from packages.semantic.policy import SemanticError


@pytest.fixture
def directory():
    parent = assets.safe_path(assets.ROOT / "runtime/temp")
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="native-owned-") as name:
        result = assets.safe_path(Path(name))
        assert result.resolve().is_relative_to(parent.resolve())
        yield result


def execute(directory, code, *, check=lambda: None, seconds=10, **options):
    return runtime.run(
        [sys.executable, "-c", code],
        directory=directory,
        environment=os.environ.copy(),
        check=check,
        deadline=time.monotonic() + seconds,
        limit=4096,
        **options,
    )


def test_exact_bytes_and_allowed_exit(directory):
    result = execute(
        directory, "import os,sys;os.write(1,b'\\x00\\xffnative\\n');sys.exit(7)", codes=(7,)
    )
    assert result.data == b"\x00\xffnative\n"
    assert result.returncode == 7
    assert result.cleanup_complete
    assert not list(directory.glob(".native-*"))


def test_nonallowed_exit_is_not_success(directory):
    with pytest.raises(RepositoryError) as error:
        execute(directory, "raise SystemExit(7)")
    assert error.value.code == "REPOSITORY_PARSE_FAILED"
    assert not list(directory.glob(".native-*"))


@pytest.mark.parametrize("allow_limit", [False, True])
def test_actual_stdout_limit(directory, allow_limit):
    if allow_limit:
        result = execute(directory, "import os;os.write(1,b'x'*4097)", allow_limit=True)
        assert result.data is None and result.cleanup_complete
    else:
        with pytest.raises(RepositoryError) as error:
            execute(directory, "import os;os.write(1,b'x'*4097)")
        assert error.value.code == "REPOSITORY_PARSE_RESOURCE_LIMIT"
    assert not list(directory.glob(".native-*"))


def test_stderr_limit_not_bypassed_by_allow_limit(directory):
    with pytest.raises(RepositoryError) as error:
        execute(
            directory,
            "import os;os.write(1,b'x'*4097);os.write(2,b'e'*65537)",
            allow_limit=True,
        )
    assert error.value.code == "REPOSITORY_OUTPUT_LIMIT"


def test_phase_observer_refusal_still_cleans_actual_owned_tree(directory):
    code, record = nested_code(directory)
    observed = []

    def observe(pid, created, descendants):
        observed.append((pid, created, descendants))
        if record.is_file():
            raise RuntimeError("sampled phase limit")

    with pytest.raises(RuntimeError, match="sampled phase limit"):
        execute(directory, code, observe=observe)
    assert observed and observed[0][1] is not None
    assert_observed_children_stopped(record)
    assert not list(directory.glob(".native-*"))


def test_failed_invocation_retains_bounded_diagnostics_before_stream_cleanup(directory):
    captured = []
    with pytest.raises(RepositoryError):
        execute(
            directory,
            "import os,sys;os.write(1,b'progress');os.write(2,b'failure');sys.exit(1)",
            capture=lambda stdout, stderr, truncated: captured.append((stdout, stderr, truncated)),
        )
    assert captured == [(b"progress", b"failure", [False, False])]
    assert not list(directory.glob(".native-*"))


def test_long_phase_retires_confirmed_exited_children_instead_of_hitting_lifetime_cap(
    directory, monkeypatch
):
    # Windows venv launchers add live wrapper processes; keep that genuine
    # concurrency within the test bound while exceeding its lifetime count.
    monkeypatch.setattr(runtime, "MAX_CHILDREN", 6)
    code = (
        "import subprocess,sys;"
        "[(subprocess.run([sys.executable,'-c','import time;time.sleep(.08)'],check=True)) "
        "for _ in range(12)];print('all twelve sequential children completed')"
    )
    result = execute(directory, code, seconds=20)
    assert result.data.splitlines() == [b"all twelve sequential children completed"]
    assert result.observed_processes >= 13
    assert result.cleanup_complete


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX unreaped-child state")
def test_unreaped_posix_children_are_not_reregistered_as_live(directory, monkeypatch):
    monkeypatch.setattr(runtime, "MAX_CHILDREN", 6)
    code = """import subprocess,sys,time,psutil
children=[]
for number in range(12):
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.08)'])
 children.append(child)
 stop=time.monotonic()+3
 while psutil.Process(child.pid).status()!=psutil.STATUS_ZOMBIE:
  if time.monotonic()>stop:raise RuntimeError('expected actual unreaped child')
  time.sleep(.005)
print('all twelve unreaped children completed',flush=True)
time.sleep(.1)
"""
    result = execute(directory, code, seconds=20)
    assert result.data == b"all twelve unreaped children completed\n"
    assert result.cleanup_complete
    assert not list(directory.glob(".native-*"))


def nested_code(directory):
    record = directory / "descendants.json"
    child = (
        "import subprocess,sys,time,json,os;from pathlib import Path;import psutil;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "Path(sys.argv[1]).write_text(json.dumps([[os.getpid(),psutil.Process().create_time()],"
        "[p.pid,psutil.Process(p.pid).create_time()]]));time.sleep(60)"
    )
    return (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r},{str(record)!r}]);time.sleep(60)"
    ), record


def assert_observed_children_stopped(record):
    assert record.is_file(), "The real child and grandchild must actually have started."
    identities = json.loads(record.read_text())
    assert len(identities) == 2
    for pid, created in identities:
        try:
            process = psutil.Process(pid)
            assert process.create_time() != created or process.status() in {
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            }
        except psutil.NoSuchProcess:
            pass


def test_real_inner_deadline_cleans_child_and_grandchild(directory):
    code, record = nested_code(directory)
    with pytest.raises(RepositoryError) as error:
        execute(directory, code, seconds=5)
    assert error.value.code == "REPOSITORY_PARSE_TIMEOUT"
    assert_observed_children_stopped(record)
    assert not list(directory.glob(".native-*"))


def test_cancel_with_tree_utility_denied_still_cleans_owned_tree(directory, monkeypatch):
    code, record = nested_code(directory)
    ready = 0

    def check():
        nonlocal ready
        if record.exists():
            ready += 1
        if ready >= 3:
            raise ValueError("synthetic cancellation")

    def denied(process):
        raise PermissionError("synthetic tree utility denial")

    monkeypatch.setattr(runtime, "_try_tree_kill", denied)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(ValueError, match="synthetic cancellation"):
            execute(directory, code, check=check)
        assert_observed_children_stopped(record)
        assert unrelated.poll() is None, "A sibling process is not owned by the command."
        assert not list(directory.glob(".native-*"))
    finally:
        unrelated.kill()
        unrelated.wait(timeout=10)


@pytest.mark.parametrize("burst", ["stdout", "stderr", "cancel"])
def test_final_poll_burst_or_supervision_is_checked(directory, monkeypatch, burst):
    original = subprocess.Popen
    revoked = False

    class Terminal:
        def __init__(self, arguments, **options):
            self.actual = original(arguments, **options)
            self.output, self.errors = options["stdout"], options["stderr"]
            self.flushed = False

        def __getattr__(self, name):
            return getattr(self.actual, name)

        def poll(self):
            nonlocal revoked
            code = self.actual.poll()
            if code is not None and not self.flushed:
                self.flushed = True
                if burst == "cancel":
                    revoked = True
                else:
                    stream = self.output if burst == "stdout" else self.errors
                    stream.write(b"x" * (4097 if burst == "stdout" else 65537))
                    stream.flush()
            return code

    def check():
        if revoked:
            raise ValueError("terminal supervision revoked")

    monkeypatch.setattr(runtime.subprocess, "Popen", Terminal)
    if burst == "cancel":
        with pytest.raises(ValueError, match="terminal supervision revoked"):
            execute(directory, "pass", check=check)
    else:
        with pytest.raises(RepositoryError) as error:
            execute(directory, "pass")
        assert error.value.code == (
            "REPOSITORY_PARSE_RESOURCE_LIMIT" if burst == "stdout" else "REPOSITORY_OUTPUT_LIMIT"
        )
    assert not list(directory.glob(".native-*"))


def test_unsafe_output_directory_is_rejected_before_launch(directory, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(assets.AssetError, match="ASSET_PATH_OUTSIDE_RUNTIME"):
        runtime.run(
            [sys.executable, "-c", "pass"],
            directory=directory / ".." / ".." / ".." / ".." / "..",
            environment=os.environ.copy(),
            check=lambda: None,
            deadline=time.monotonic() + 5,
            limit=4096,
        )
    assert not calls


def test_root_identity_drift_never_enumerates_replacement_children(directory, monkeypatch):
    root = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    owned = runtime._Owned(root)
    owned.sample()
    original = psutil.Process
    calls = []

    class Replaced:
        def create_time(self):
            return owned.root_created + 1

        def children(self, **options):
            calls.append(options)
            raise AssertionError("A reused root PID must not enumerate another tree.")

    def lookup(pid=None):
        return Replaced() if pid == root.pid else original(pid)

    monkeypatch.setattr(runtime.psutil, "Process", lookup)
    try:
        with pytest.raises(RepositoryError) as error:
            owned.sample()
        assert error.value.code == "NATIVE_PROCESS_IDENTITY_CHANGED"
        assert not calls
    finally:
        # Restore the actual class before psutil's own isinstance/kill internals run.
        monkeypatch.setattr(runtime.psutil, "Process", original)
        with pytest.raises(RepositoryError) as error:
            owned.close()
        assert error.value.code == "NATIVE_PROCESS_CLEANUP_FAILED"
        assert root.returncode is not None


def test_normal_root_exit_also_cleans_observed_descendants(directory):
    record = directory / "normal-descendant.json"
    code = (
        "import subprocess,sys,time,json;from pathlib import Path;import psutil;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"Path({str(record)!r}).write_text(json.dumps([[p.pid,psutil.Process(p.pid).create_time()]]));"
        "time.sleep(0.5)"
    )
    result = execute(directory, code)
    assert result.returncode == 0 and result.cleanup_complete
    assert result.observed_processes >= 2
    for pid, created in json.loads(record.read_text()):
        try:
            process = psutil.Process(pid)
            assert process.create_time() != created or process.status() in {
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            }
        except psutil.NoSuchProcess:
            pass


def test_unexpected_descendant_cleanup_error_cannot_skip_root_handle(directory, monkeypatch):
    root = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    owned = runtime._Owned(root)
    end = time.monotonic() + 5
    while not owned.known and time.monotonic() < end:
        owned.sample()
        time.sleep(0.02)
    assert owned.known
    original = runtime._kill_identity

    def failed_identity(pid, created):
        raise RuntimeError("synthetic descendant cleanup implementation failure")

    def denied(process):
        raise PermissionError("synthetic optional utility failure")

    monkeypatch.setattr(runtime, "_kill_identity", failed_identity)
    monkeypatch.setattr(runtime, "_try_tree_kill", denied)
    try:
        with pytest.raises(RepositoryError) as error:
            owned.close()
        assert error.value.code == "NATIVE_PROCESS_CLEANUP_FAILED"
        assert root.returncode is not None
    finally:
        for pid, created in owned.known.items():
            original(pid, created)
        if root.poll() is None:
            root.kill()
        root.wait(timeout=10)


@pytest.mark.parametrize("result,expected", [(0, False), (0x102, True)])
def test_windows_signal_query_closes_minimal_rights_handle(monkeypatch, result, expected):
    closed = []

    class API:
        def OpenProcess(self, rights, inherited, pid):
            assert rights == 0x00100000 and inherited is False and pid == 123
            return 77

        def WaitForSingleObject(self, handle, timeout):
            assert handle == 77 and timeout == 0
            return result

        def CloseHandle(self, handle):
            closed.append(handle)
            return True

    monkeypatch.setattr(runtime, "_windows", API())
    assert runtime._windows_active(123) is expected
    assert closed == [77]


@pytest.mark.parametrize("failure", ["missing", "denied", "wait", "close"])
def test_windows_signal_failure_cannot_certify_liveness_or_skip_handle_cleanup(
    monkeypatch, failure
):
    closed = []

    class API:
        def OpenProcess(self, rights, inherited, pid):
            return None if failure in {"missing", "denied"} else 77

        def WaitForSingleObject(self, handle, timeout):
            return 0xFFFFFFFF if failure == "wait" else 0x102

        def CloseHandle(self, handle):
            closed.append(handle)
            return failure != "close"

    monkeypatch.setattr(runtime, "_windows", API())
    monkeypatch.setattr(runtime, "_windows_error", lambda: 87 if failure == "missing" else 5)
    if failure == "missing":
        assert runtime._windows_active(123) is False
    else:
        with pytest.raises(RepositoryError, match="NATIVE_PROCESS_"):
            runtime._windows_active(123)
    assert closed == ([] if failure in {"missing", "denied"} else [77])


def test_real_windows_process_signal_and_pid_binding(directory):
    if os.name != "nt":
        process = psutil.Process()
        assert runtime._live(process.pid, process.create_time())
        assert not runtime._live(process.pid, process.create_time() + 1)
        return
    with subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdin.readline();sys.exit(259)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=directory,
    ) as process:
        owned = runtime._Owned(process)
        try:
            owned.sample()
            created = psutil.Process(process.pid).create_time()
            assert runtime._live(process.pid, created)
            assert not runtime._live(process.pid, created + 1)
            before = psutil.Process().num_handles()
            for _ in range(200):
                assert runtime._windows_active(process.pid)
            assert psutil.Process().num_handles() <= before + 2
            process.stdin.write(b"\n")
            process.stdin.flush()
            assert process.wait(timeout=5) == 259
            assert runtime._windows_active(process.pid) is False
            assert runtime._live(process.pid, created) is False
        finally:
            owned.close()


def test_new_parent_checkpoint_rejects_identity_before_liveness_query(monkeypatch):
    current = psutil.Process()
    monkeypatch.setenv("DG_SEMANTIC_PARENT", str(current.pid))
    monkeypatch.setenv("DG_SEMANTIC_PARENT_TIME", str(current.create_time() + 1))

    def unexpected(process):
        pytest.fail("A replacement parent identity must not be queried as its predecessor")

    monkeypatch.setattr(runtime, "_active", unexpected)
    with pytest.raises(SemanticError, match="SEMANTIC_PARENT_GONE"):
        runtime.checkpoint()


def test_parent_observation_failure_is_fatal_semantic_error(monkeypatch):
    current = psutil.Process()
    monkeypatch.setenv("DG_SEMANTIC_PARENT", str(current.pid))
    monkeypatch.setenv("DG_SEMANTIC_PARENT_TIME", str(current.create_time()))

    def failed(process):
        raise RepositoryError(422, "NATIVE_PROCESS_HANDLE_CLEANUP_FAILED")

    monkeypatch.setattr(runtime, "_active", failed)
    with pytest.raises(SemanticError, match="NATIVE_PROCESS_HANDLE_CLEANUP_FAILED"):
        runtime.checkpoint()
