"""Supervised offline source supplement; no model fitting or qualification promotion."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import psutil

from packages.mining import public_assets as assets
from packages.repositories.safety import RepositoryError
from packages.semantic import native_runtime
from packages.semantic import source_resolution as resolution
from packages.semantic.native_runtime import checkpoint
from packages.semantic.policy import SemanticError


def supervised(arguments, log, *, seconds=2700, rss_limit=6 * 1024**3):
    log = assets.safe_path(Path(log))
    log.parent.mkdir(parents=True, exist_ok=True)
    resolution.original.require(
        not log.exists() and not log.with_suffix(log.suffix + ".json").exists(),
        "SOURCE_LOG_EXISTS",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DG_") and key not in {"GIT_SSH_COMMAND", "SSH_AUTH_SOCK"}
    }
    parent = psutil.Process()
    environment.update(
        DG_SEMANTIC_PARENT=str(parent.pid),
        DG_SEMANTIC_PARENT_TIME=str(parent.create_time()),
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    peak = 0
    started = time.monotonic()
    reason = "SOURCE_PHASE_INTERRUPTED"
    completed = False
    capture_state = {"captured": False}
    observed_root = None
    live_descendants = {}
    work = log.parent / (".supervision-" + uuid4().hex)
    work.mkdir()

    def observe(pid, created, descendants):
        nonlocal peak, observed_root, live_descendants
        observed_root = {"pid": pid, "created": created}
        live_descendants = dict(descendants)
        current = 0
        for identity, expected in ({pid: created} | descendants).items():
            if expected is None:
                continue
            try:
                process = psutil.Process(identity)
                if process.create_time() == expected:
                    current += process.memory_info().rss
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:
                raise SemanticError("SOURCE_PHASE_OBSERVATION_FAILED") from exc
        peak = max(peak, current)
        if current > rss_limit:
            raise SemanticError("SOURCE_PHASE_MEMORY_LIMIT")

    def capture(stdout, stderr, truncated):
        with log.open("xb") as stream:
            stream.write(stdout)
            if stderr:
                stream.write(b"\n[bounded child stderr]\n" + stderr)
        capture_state.update(
            captured=True,
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
            stdout_truncated=truncated[0],
            stderr_truncated=truncated[1],
        )

    try:
        result = native_runtime.run(
            [
                sys.executable,
                "-B",
                "-m",
                "scripts.semantic_source_resolution",
                *arguments,
                "--child",
            ],
            directory=work,
            environment=environment | {"PYTHONPATH": str(assets.ROOT)},
            check=checkpoint,
            deadline=started + seconds,
            limit=8 * 1024**2 - 65536 - 1024,
            observe=observe,
            capture=capture,
        )
        completed, reason = True, None
        return result
    except (RepositoryError, SemanticError) as exc:
        reason = getattr(exc, "code", str(exc))
        raise
    finally:
        cleanup_error = None
        try:
            work.rmdir()
        except OSError as exc:
            completed, reason = False, "SOURCE_SUPERVISION_CLEANUP_FAILED"
            cleanup_error = exc
        proof = {
            "completed": completed,
            "reason": reason,
            "peak_child_rss_bytes": peak,
            "seconds": time.monotonic() - started,
            "cleanup_required_before_success": True,
            "capture": capture_state,
            "observed_root": observed_root,
            "last_live_descendants": live_descendants,
            "scope": "owned tree/group and sampled RSS, not every OS crash or a hard quota",
        }
        resolution.original._atomic(log.with_suffix(log.suffix + ".json"), proof)
        # Unexpected files are preserved, never recursively deleted.
        if cleanup_error:
            raise SemanticError("SOURCE_SUPERVISION_CLEANUP_FAILED") from cleanup_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["safety", "declare", "benchmark", "resolve", "summarize"])
    for name in (
        "source",
        "audit",
        "plan",
        "declaration",
        "cache",
        "inputs",
        "output",
        "log",
        "result",
        "safety",
        "benchmark",
    ):
        parser.add_argument("--" + name, type=Path, required=name in {"output", "log", "result"})
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    required = {
        "safety": (),
        "declare": ("source", "audit", "plan"),
        "benchmark": ("declaration", "source", "audit", "safety"),
        "resolve": ("source", "audit", "declaration", "cache"),
        "summarize": ("inputs",),
    }[args.phase]
    for name in required:
        if getattr(args, name) is None:
            parser.error("--" + name + " is required")
    if args.pilot and args.phase != "resolve":
        parser.error("--pilot is resolve-only")
    allowed_inputs = set(required) | ({"benchmark"} if args.phase == "resolve" else set())
    for name in (
        "source",
        "audit",
        "plan",
        "declaration",
        "cache",
        "inputs",
        "safety",
        "benchmark",
    ):
        if getattr(args, name) is not None and name not in allowed_inputs:
            parser.error("--" + name + " is not used by " + args.phase)
    paths = resolution._separate(
        args.result,
        args.log,
        args.output,
        args.log.with_suffix(args.log.suffix + ".json"),
        *[
            getattr(args, name)
            for name in dict.fromkeys((*required, "benchmark"))
            if getattr(args, name) is not None
        ],
    )
    result, log, output = paths[:3]
    if args.child:
        resolution.original.require(
            os.environ.get("DG_SEMANTIC_PARENT"), "SOURCE_CHILD_PARENT_REQUIRED"
        )
        checkpoint()
        if args.phase == "safety":
            from packages.semantic import source_benchmark as measurement

            target = measurement.safety(output)
        elif args.phase == "declare":
            target = resolution.declare(args.source, args.audit, args.plan, output)
        elif args.phase == "benchmark":
            from packages.semantic import source_benchmark as measurement

            target = measurement.benchmark(
                args.declaration, args.source, args.audit, args.safety, output
            )
        elif args.phase == "resolve":
            target = resolution.resolve(
                args.declaration,
                args.source,
                args.audit,
                args.cache,
                output,
                pilot=args.pilot,
                benchmark=args.benchmark,
            )
        else:
            target = resolution.summarize(args.inputs, output)
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    resolution.original.require(not result.exists(), "SOURCE_RESULT_EXISTS")
    completed = supervised(
        sys.argv[1:],
        log,
        seconds=600 if args.phase == "safety" else 1800 if args.phase == "benchmark" else 2700,
    )
    answer = None
    for line in reversed(completed.data.splitlines()):
        if line.startswith(b'{"directory":'):
            answer = json.loads(line)
            break
    resolution.original.require(answer is not None, "SOURCE_RESULT_MISSING")
    directory = assets.safe_path(Path(answer["directory"]))
    resolution.original.require(directory.parent == output, "SOURCE_RESULT_DRIFT")
    manifest = resolution.original.artifacts.verify(directory, checkpoint)
    if args.phase in {"safety", "benchmark"}:
        from packages.semantic import source_benchmark as measurement

        if args.phase == "safety":
            measurement.verify_safety(directory, checkpoint)
        else:
            measurement.verify_benchmark(directory, checkpoint)
    proof = assets.read_json(log.with_suffix(log.suffix + ".json"), checkpoint)
    result.parent.mkdir(parents=True, exist_ok=True)
    resolution.original._atomic(
        result, answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(answer | {"supervision": proof}))


if __name__ == "__main__":
    try:
        main()
    except (RepositoryError, SemanticError, assets.AssetError) as exc:
        print(json.dumps({"error": getattr(exc, "code", str(exc))}), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
