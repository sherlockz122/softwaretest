"""Supervised immutable96 content-aware contexts and independent frozen encoding."""

import argparse
import json
import os
import sys
from pathlib import Path

from packages.mining import public_assets as assets
from packages.semantic import artifacts, policy
from packages.semantic.control import supervise
from packages.semantic.policy import SemanticError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("declare", "contexts", "encode", "compare"))
    for name in (
        "inputs",
        "checkpoint",
        "declaration",
        "source",
        "other",
        "output",
        "result",
        "log",
    ):
        parser.add_argument(
            "--" + name, type=Path, required=name in {"inputs", "output", "result", "log"}
        )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for name in {
        "declare": ("checkpoint",),
        "contexts": ("checkpoint", "declaration", "source"),
        "encode": ("checkpoint",),
        "compare": ("other",),
    }[args.phase]:
        if getattr(args, name) is None:
            parser.error("--" + name + " is required for " + args.phase)
    if args.child:
        from packages.semantic import adaptive, adaptive_encoder

        if args.phase == "declare":
            target = adaptive.declare(args.inputs, args.checkpoint, args.output)
        elif args.phase == "contexts":
            target = adaptive.contexts(
                args.inputs, args.declaration, args.source, args.checkpoint, args.output
            )
        elif args.phase == "encode":
            target = adaptive_encoder.encode(args.inputs, args.checkpoint, args.output)
        else:
            target = adaptive_encoder.compare(args.inputs, args.other, args.output)
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    result = assets.safe_path(args.result)
    if result.exists():
        raise SemanticError("ADAPTIVE_RESULT_EXISTS")
    interpreter = (
        sys.executable
        if args.phase in {"declare", "compare"}
        else str(
            assets.ROOT
            / "tools/semantic/.venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
    )
    proof = supervise(
        ["-m", "scripts.semantic_adaptive_probe", *sys.argv[1:], "--child"],
        args.log,
        interpreter=interpreter,
    )
    if (
        not proof["completed"]
        or proof["seconds"] > policy.MAX_SECONDS
        or proof["peak_child_rss_bytes"] > policy.MAX_RSS
    ):
        raise SemanticError("ADAPTIVE_FINAL_SUPERVISION_LIMIT")
    answer = None
    for line in reversed(args.log.read_bytes().splitlines()):
        if line.startswith(b'{"directory":'):
            answer = json.loads(line.decode("ascii"))
            break
    if answer is None:
        raise SemanticError("ADAPTIVE_RESULT_MISSING")
    manifest = artifacts.verify(Path(answer["directory"]))
    result.parent.mkdir(parents=True, exist_ok=True)
    assets.write_json(
        result, answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(answer | {"supervision": proof}))


if __name__ == "__main__":
    main()
