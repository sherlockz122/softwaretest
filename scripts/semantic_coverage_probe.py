"""Supervised, immutable selection/context/frozen-window engineering probes."""

import argparse
import json
import os
import sys
from pathlib import Path

from packages.mining import public_assets as assets
from packages.semantic import artifacts, policy
from packages.semantic.control import supervise
from packages.semantic.policy import SemanticError


def read_result(log):
    for line in reversed(log.read_bytes().splitlines()):
        if line.startswith(b'{"directory":'):
            return json.loads(line.decode("ascii"))
    raise SemanticError("COVERAGE_RESULT_MISSING")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("select", "contexts", "encode", "compare"))
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--other", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for name in {
        "select": ("checkpoint",),
        "contexts": ("checkpoint", "declaration", "source"),
        "encode": ("checkpoint",),
        "compare": ("other",),
    }[args.phase]:
        if getattr(args, name) is None:
            parser.error("--" + name + " is required for " + args.phase)
    if args.child:
        from packages.semantic import coverage, coverage_encoder

        if args.phase == "select":
            target = coverage.declare(args.inputs, args.checkpoint, args.output)
        elif args.phase == "contexts":
            target = coverage.contexts(
                args.inputs, args.declaration, args.source, args.checkpoint, args.output
            )
        elif args.phase == "encode":
            target = coverage_encoder.encode(args.inputs, args.checkpoint, args.output)
        else:
            target = coverage_encoder.compare(args.inputs, args.other, args.output)
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    result = assets.safe_path(args.result)
    if result.exists():
        raise ValueError("COVERAGE_RESULT_EXISTS")
    interpreter = (
        sys.executable
        if args.phase in {"select", "compare"}
        else str(
            assets.ROOT
            / "tools/semantic/.venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
    )
    proof = supervise(
        ["-m", "scripts.semantic_coverage_probe", *sys.argv[1:], "--child"],
        args.log,
        interpreter=interpreter,
    )
    if proof["seconds"] > policy.MAX_SECONDS or proof["peak_child_rss_bytes"] > policy.MAX_RSS:
        raise SemanticError("COVERAGE_FINAL_SUPERVISION_LIMIT")
    answer = read_result(args.log)
    manifest = artifacts.verify(Path(answer["directory"]))
    result.parent.mkdir(parents=True, exist_ok=True)
    assets.write_json(
        result, answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(answer | {"supervision": proof}))


if __name__ == "__main__":
    main()
