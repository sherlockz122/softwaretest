"""Bounded owned children for the new source path; legacy runtimes stay frozen."""

import ctypes
import math
import os
import shutil
import signal
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psutil

from packages.mining import public_assets as assets
from packages.repositories.safety import RepositoryError

MAX_CHILDREN = 128
STDERR_LIMIT = 65536
_windows = None
if os.name == "nt":
    _windows = ctypes.WinDLL("kernel32", use_last_error=True)
    _windows.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _windows.OpenProcess.restype = wintypes.HANDLE
    _windows.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _windows.WaitForSingleObject.restype = wintypes.DWORD
    _windows.CloseHandle.argtypes = (wintypes.HANDLE,)
    _windows.CloseHandle.restype = wintypes.BOOL


def _windows_error():
    return ctypes.get_last_error()


def _windows_active(pid):
    """Only query a process signal; never obtain terminate/ACL/debug rights."""
    handle = _windows.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        if _windows_error() == 87:  # Process identity disappeared before opening.
            return False
        raise RepositoryError(422, "NATIVE_PROCESS_OBSERVATION_FAILED")
    try:
        result = _windows.WaitForSingleObject(handle, 0)
        if result == 0:  # Signaled, including an exited process with retained handles.
            return False
        if result == 0x102:  # WAIT_TIMEOUT: process has not signaled termination.
            return True
        raise RepositoryError(422, "NATIVE_PROCESS_OBSERVATION_FAILED")
    finally:
        if not _windows.CloseHandle(handle):
            raise RepositoryError(422, "NATIVE_PROCESS_HANDLE_CLEANUP_FAILED")


def _active(process):
    if os.name == "nt":
        return _windows_active(process.pid)
    return process.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}


def checkpoint():
    """New-path parent supervision; frozen semantic.control is not replaced."""
    from packages.semantic.policy import SemanticError

    if shutil.disk_usage(assets.ROOT).free < 2.5 * 1024**3:
        raise SemanticError("SEMANTIC_CAPACITY_UNSAFE")
    pid = os.environ.get("DG_SEMANTIC_PARENT")
    if pid:
        try:
            parent = psutil.Process(int(pid))
            if parent.create_time() != float(os.environ["DG_SEMANTIC_PARENT_TIME"]) or not _active(
                parent
            ):
                raise SemanticError("SEMANTIC_PARENT_GONE")
        except RepositoryError as exc:
            # Unsafe supervision failures must never become ordinary parse gaps.
            raise SemanticError(exc.code) from exc
        except (psutil.Error, ValueError, KeyError) as exc:
            raise SemanticError("SEMANTIC_PARENT_GONE") from exc


def _try_tree_kill(process):
    if os.name == "nt" and process.poll() is None:
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )


def _live(pid, created):
    try:
        process = psutil.Process(pid)
        return process.create_time() == created and _active(process)
    except psutil.NoSuchProcess:
        return False


def _kill_identity(pid, created):
    try:
        child = psutil.Process(pid)
        if child.create_time() == created:
            child.kill()
    except psutil.NoSuchProcess:
        pass


class _Owned:
    def __init__(self, process):
        self.process = process
        self.known = {}
        self.observed_total = 0
        self.observation_failed = False
        self.root_created = None
        self.group_closed = False

    def sample(self):
        try:
            parent = psutil.Process(self.process.pid)
            created = parent.create_time()
            if self.root_created is None:
                self.root_created = created
            elif self.root_created != created:
                self.observation_failed = True
                raise RepositoryError(422, "NATIVE_PROCESS_IDENTITY_CHANGED")
            # The bound is on the live owned registry, not on all Git processes
            # ever created by a long source phase. Retire only identities proven
            # terminal or no longer matching their original creation time.
            self.known = {
                pid: started for pid, started in self.known.items() if _live(pid, started)
            }
            for child in parent.children(recursive=True):
                try:
                    identity = child.create_time()
                    if not _active(child):
                        continue
                    if self.known.get(child.pid) != identity:
                        self.observed_total += 1
                    self.known[child.pid] = identity
                except psutil.NoSuchProcess:
                    continue
                if len(self.known) > MAX_CHILDREN:
                    self.observation_failed = True
                    raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            self.observation_failed = True
            raise RepositoryError(422, "NATIVE_PROCESS_OBSERVATION_FAILED") from None

    def kill_group(self):
        if os.name != "nt" and not self.group_closed:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.group_closed = True

    def poll(self):
        if os.name == "nt" or self.group_closed:
            return self.process.poll()
        # Keep the exited group leader waitable until its owned group is killed.
        # Reaping first could allow an unrelated group to reuse its numeric ID.
        if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
            completed = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if completed is None:
                return None
        else:
            try:
                if psutil.Process(self.process.pid).status() != psutil.STATUS_ZOMBIE:
                    return None
            except psutil.NoSuchProcess:
                self.observation_failed = True
                raise RepositoryError(422, "NATIVE_PROCESS_IDENTITY_CHANGED") from None
        self.kill_group()
        return self.process.poll()

    def close(self):
        cleanup_failed = False
        try:
            try:
                self.sample()
            except BaseException:
                self.observation_failed = True
            # Optional tree utilities must not prevent the owned-handle fallback.
            try:
                _try_tree_kill(self.process)
            except (OSError, subprocess.SubprocessError):
                pass
            except BaseException:
                cleanup_failed = True
            if os.name != "nt":
                try:
                    self.kill_group()
                except BaseException:
                    cleanup_failed = True
            for pid, created in sorted(self.known.items(), key=lambda item: item[1], reverse=True):
                try:
                    _kill_identity(pid, created)
                except BaseException:
                    cleanup_failed = True
        finally:
            # Even an unexpected descendant-cleanup error cannot skip this handle.
            try:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
        stop = time.monotonic() + 10
        while True:
            try:
                remaining = [pid for pid, created in self.known.items() if _live(pid, created)]
            except BaseException:
                cleanup_failed = True
                break
            if not remaining:
                break
            if time.monotonic() >= stop:
                cleanup_failed = True
                break
            time.sleep(0.02)
        if cleanup_failed or self.observation_failed:
            raise RepositoryError(422, "NATIVE_PROCESS_CLEANUP_FAILED")


@dataclass(frozen=True)
class Result:
    data: bytes | None
    returncode: int
    seconds: float
    observed_processes: int
    cleanup_complete: bool


def run(
    arguments,
    *,
    directory,
    environment,
    check,
    deadline,
    limit,
    codes=(0,),
    allow_limit=False,
    observe=None,
    capture=None,
):
    """Internal invocation only; callers separately constrain the Git command set."""
    directory = assets.safe_path(Path(directory))
    if (
        not directory.is_dir()
        or type(limit) is not int
        or not 0 < limit <= 8 * 1024**2
        or type(deadline) not in (int, float)
        or not math.isfinite(deadline)
        or not arguments
        or len(arguments) > 256
        or any(not isinstance(argument, str) or "\0" in argument for argument in arguments)
        or sum(len(argument.encode("utf-8")) for argument in arguments) > 1024**2
        or not codes
        or any(type(code) is not int for code in codes)
        or type(allow_limit) is not bool
        or observe is not None
        and not callable(observe)
        or capture is not None
        and not callable(capture)
    ):
        raise RepositoryError(422, "NATIVE_COMMAND_INVALID")
    started = time.monotonic()

    def supervise():
        check()
        if time.monotonic() >= deadline:
            raise RepositoryError(422, "REPOSITORY_PARSE_TIMEOUT")

    supervise()
    nonce = uuid4().hex
    output = directory / (".native-" + nonce + ".out")
    errors = directory / (".native-" + nonce + ".err")
    owned = None
    limited = False
    try:
        try:
            with output.open("xb") as stdout, errors.open("xb") as stderr:
                process = subprocess.Popen(
                    arguments,
                    cwd=directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                owned = _Owned(process)
                while True:
                    owned.sample()
                    if observe is not None:
                        observe(process.pid, owned.root_created, dict(owned.known))
                    supervise()
                    if errors.stat().st_size > STDERR_LIMIT:
                        raise RepositoryError(422, "REPOSITORY_OUTPUT_LIMIT")
                    if output.stat().st_size > limit:
                        if not allow_limit:
                            raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
                        limited = True
                        break
                    if owned.poll() is not None:
                        break
                    time.sleep(0.02)
        finally:
            if owned is not None:
                owned.close()
        # Closed streams and cleaned descendants precede terminal acceptance.
        supervise()
        if errors.stat().st_size > STDERR_LIMIT:
            raise RepositoryError(422, "REPOSITORY_OUTPUT_LIMIT")
        if limited or output.stat().st_size > limit:
            if allow_limit:
                data = None
            else:
                raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
        else:
            if process.returncode not in codes:
                raise RepositoryError(422, "REPOSITORY_PARSE_FAILED")
            with output.open("rb") as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                if allow_limit:
                    data = None
                else:
                    raise RepositoryError(422, "REPOSITORY_PARSE_RESOURCE_LIMIT")
        return Result(
            data, process.returncode, time.monotonic() - started, owned.observed_total + 1, True
        )
    finally:
        try:
            if capture is not None and owned is not None:
                captured = []
                truncated = []
                for path, maximum in ((output, limit), (errors, STDERR_LIMIT)):
                    with path.open("rb") as stream:
                        raw = stream.read(maximum + 1)
                    captured.append(raw[:maximum])
                    truncated.append(len(raw) > maximum)
                capture(*captured, truncated)
        finally:
            failed = False
            for path in (output, errors):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    failed = True
            if failed:
                raise RepositoryError(422, "NATIVE_STREAM_CLEANUP_FAILED")
