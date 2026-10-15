"""CPU child supervision and parent identity checks, isolated from application workers."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from packages.semantic import policy
from packages.semantic.policy import SemanticError


def terminate(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def checkpoint():
    if shutil.disk_usage(Path.cwd()).free < 2.5 * 1024**3:
        raise SemanticError("SEMANTIC_CAPACITY_UNSAFE")
    pid = os.environ.get("DG_SEMANTIC_PARENT")
    if pid:
        try:
            parent = psutil.Process(int(pid))
            if parent.status() in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                raise SemanticError("SEMANTIC_PARENT_GONE")
            if parent.create_time() != float(os.environ["DG_SEMANTIC_PARENT_TIME"]):
                raise SemanticError("SEMANTIC_PARENT_GONE")
        except psutil.Error:
            raise SemanticError("SEMANTIC_PARENT_GONE") from None


def supervise(
    arguments,
    log,
    *,
    interpreter=sys.executable,
    seconds=policy.MAX_SECONDS,
    rss_limit=policy.MAX_RSS,
    observe=None,
):
    from packages.mining.public_assets import safe_path

    log = safe_path(Path(log))
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        raise SemanticError("SEMANTIC_LOG_EXISTS")
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("DG_") and k not in {"GIT_SSH_COMMAND", "SSH_AUTH_SOCK"}
    }
    parent = psutil.Process()
    environment.update(
        DG_SEMANTIC_PARENT=str(parent.pid),
        DG_SEMANTIC_PARENT_TIME=str(parent.create_time()),
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
    )
    started, peak, reason = time.monotonic(), 0, None
    with log.open("xb") as stream:
        process = subprocess.Popen(
            [interpreter, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=environment,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            while process.poll() is None:
                checkpoint()
                if time.monotonic() - started > seconds:
                    raise SemanticError("SEMANTIC_DEADLINE")
                try:
                    child = psutil.Process(process.pid)
                    current = child.memory_info().rss + sum(
                        p.memory_info().rss for p in child.children(recursive=True)
                    )
                    peak = max(peak, current)
                    if current > rss_limit:
                        raise SemanticError("SEMANTIC_MEMORY_LIMIT")
                except psutil.NoSuchProcess:
                    pass
                if log.stat().st_size > 8 * 1024**2:
                    raise SemanticError("SEMANTIC_LOG_LIMIT")
                if observe:
                    observe(process)
                time.sleep(0.25)
            if process.returncode:
                raise SemanticError("SEMANTIC_CHILD_FAILED")
            checkpoint()
            if log.stat().st_size > 8 * 1024**2:
                raise SemanticError("SEMANTIC_LOG_LIMIT")
        except BaseException as exc:
            reason = str(exc) if isinstance(exc, SemanticError) else "SEMANTIC_INTERRUPTED"
            raise
        finally:
            terminate(process)
            proof = {
                "completed": reason is None and process.returncode == 0,
                "reason": reason,
                "peak_child_rss_bytes": peak,
                "seconds": time.monotonic() - started,
            }
            log.with_suffix(log.suffix + ".json").write_text(
                json.dumps(proof, indent=2), encoding="utf-8"
            )
    return proof
