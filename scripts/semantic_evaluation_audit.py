"""Supervised new source audit; no model execution, Issue network or old-test scores."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from packages.mining import public_assets as assets
from packages.repositories.safety import RepositoryError
from packages.semantic import artifacts
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError


def supervised(arguments, log, *, seconds=2700, rss_limit=6 * 1024**3):
    log = assets.safe_path(Path(log))
    log.parent.mkdir(parents=True, exist_ok=True)
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
    )
    start = time.monotonic()
    peak = 0
    reason = None
    known = {}
    with log.open("xb") as stream:
        process = subprocess.Popen(
            [sys.executable, *arguments],
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
                if time.monotonic() - start > seconds:
                    raise SemanticError("EVAL_PHASE_DEADLINE")
                try:
                    tree = [psutil.Process(process.pid)]
                    tree += tree[0].children(recursive=True)
                    current = 0
                    for child in tree:
                        known[child.pid] = child.create_time()
                        current += child.memory_info().rss
                    peak = max(peak, current)
                    if current > rss_limit:
                        raise SemanticError("EVAL_PHASE_MEMORY")
                except psutil.NoSuchProcess:
                    pass
                if log.stat().st_size > 8 * 1024**2:
                    raise SemanticError("EVAL_PHASE_LOG_LIMIT")
                time.sleep(0.1)
            checkpoint()
            if process.returncode:
                raise SemanticError("EVAL_PHASE_CHILD_FAILED")
            if time.monotonic() - start > seconds or log.stat().st_size > 8 * 1024**2:
                raise SemanticError("EVAL_FINAL_SUPERVISION_LIMIT")
        except BaseException as exc:
            reason = str(exc) if isinstance(exc, SemanticError) else "EVAL_INTERRUPTED"
            raise
        finally:
            if os.name != "nt":
                # This invocation created the session; Git inherits this owned group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                # taskkill may be denied in a restricted Windows token. The Popen
                # handle still belongs to us; do not let a failed external utility
                # skip cleanup of the identities sampled from our own subtree.
                if process.poll() is None:
                    try:
                        subprocess.run(
                            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                            capture_output=True,
                            timeout=10,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                for pid, created in known.items():
                    if pid == process.pid:
                        continue
                    try:
                        child = psutil.Process(pid)
                        if child.create_time() == created:
                            child.kill()
                    except psutil.NoSuchProcess:
                        pass
                if process.poll() is None:
                    process.kill()
            process.wait(timeout=10)
            proof = {
                "completed": reason is None and process.returncode == 0,
                "reason": reason,
                "peak_child_rss_bytes": peak,
                "seconds": time.monotonic() - start,
                "scope": (
                    "owned process group/tree, cooperative parent checks and sampled RSS; "
                    "not every OS crash or an OS resource quota"
                ),
            }
            assets.write_json(log.with_suffix(log.suffix + ".json"), proof)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["weights", "declare", "observe", "summarize"])
    for name in [
        "inputs",
        "source",
        "dataset",
        "reservation",
        "weights",
        "declaration",
        "cache",
        "output",
        "result",
        "log",
    ]:
        parser.add_argument("--" + name, type=Path, required=name in {"output", "result", "log"})
    parser.add_argument("--head")
    parser.add_argument("--pilot", type=int)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    required = {
        "weights": ["inputs"],
        "declare": ["source", "dataset", "reservation", "weights", "head"],
        "observe": ["declaration", "source", "cache"],
        "summarize": ["inputs"],
    }[args.phase]
    for name in required:
        if getattr(args, name) is None:
            parser.error("--" + name + " is required")
    if args.pilot is not None and (args.phase != "observe" or not 1 <= args.pilot <= 16):
        parser.error("--pilot is observe-only,1..16")
    result = assets.safe_path(args.result)
    if args.child:
        if not os.environ.get("DG_SEMANTIC_PARENT"):
            raise SemanticError("EVAL_CHILD_PARENT_REQUIRED")
        checkpoint()
        from packages.semantic import evaluation_population as population
        from packages.semantic import evaluation_weights as weights

        if args.phase == "weights":
            target = weights.register(args.inputs, args.output, checkpoint)
        elif args.phase == "declare":
            target = population.declare(
                args.source, args.dataset, args.reservation, args.weights, args.output, args.head
            )
        elif args.phase == "observe":
            target = population.observe(
                args.declaration, args.source, args.cache, args.output, pilot=args.pilot
            )
        else:
            target = population.summarize(args.inputs, args.output)
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    if result.exists():
        raise SemanticError("EVAL_RESULT_EXISTS")
    proof = supervised(
        ["-m", "scripts.semantic_evaluation_audit", *sys.argv[1:], "--child"], args.log
    )
    require_answer = None
    for line in reversed(args.log.read_bytes().splitlines()):
        if line.startswith(b'{"directory":'):
            require_answer = json.loads(line.decode("ascii"))
            break
    if require_answer is None:
        raise SemanticError("EVAL_RESULT_MISSING")
    manifest = artifacts.verify(Path(require_answer["directory"]), checkpoint)
    result.parent.mkdir(parents=True, exist_ok=True)
    assets.write_json(
        result, require_answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(require_answer | {"supervision": proof}))


if __name__ == "__main__":
    try:
        main()
    except SemanticError as exc:
        print(json.dumps({"status": "refused", "code": str(exc)}), flush=True)
        sys.exit(2)
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "code": "EVAL_INTERRUPTED"}), flush=True)
        sys.exit(130)
    except (assets.AssetError, RepositoryError, OSError, ValueError, TypeError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "status": "refused",
                    "code": getattr(exc, "code", "EVAL_INPUT_REJECTED"),
                    "error_type": type(exc).__name__,
                }
            ),
            flush=True,
        )
        sys.exit(2)
