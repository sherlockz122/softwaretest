"""Supervised complete660 contexts, actual84 tail encoding and exact full repeats."""

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
    parser.add_argument(
        "phase",
        choices=("declare", "contexts", "merge-contexts", "encode", "merge-embeddings", "compare"),
    )
    for name in (
        "inputs",
        "v3",
        "v4",
        "checkpoint",
        "declaration",
        "source",
        "other",
        "output",
        "result",
        "log",
    ):
        parser.add_argument("--" + name, type=Path, required=name in {"output", "result", "log"})
    parser.add_argument("--parts", type=Path, nargs="+")
    parser.add_argument("--chunk-index", type=int)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    required = {
        "declare": ("inputs", "v3", "v4", "checkpoint"),
        "contexts": ("declaration", "source", "chunk_index"),
        "merge-contexts": ("declaration", "parts"),
        "encode": ("inputs", "chunk_index"),
        "merge-embeddings": ("inputs", "parts"),
        "compare": ("inputs", "other"),
    }[args.phase]
    for name in required:
        if getattr(args, name) is None:
            parser.error("--" + name.replace("_", "-") + " is required for " + args.phase)
    if args.child:
        from packages.semantic import population as p
        from packages.semantic import population_encoder as e

        if args.phase == "declare":
            target = p.declare(args.inputs, args.v3, args.v4, args.checkpoint, args.output)
        elif args.phase == "contexts":
            target = p.contexts(args.declaration, args.source, args.output, args.chunk_index)
        elif args.phase == "merge-contexts":
            target = p.merge_contexts(args.declaration, args.parts, args.output)
        elif args.phase == "encode":
            target = e.encode(args.inputs, args.output, args.chunk_index)
        elif args.phase == "merge-embeddings":
            target = e.merge(args.inputs, args.parts, args.output)
        else:
            target = e.compare(args.inputs, args.other, args.output)
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    result = assets.safe_path(args.result)
    if result.exists():
        raise SemanticError("POPULATION_RESULT_EXISTS")
    interpreter = (
        sys.executable
        if args.phase not in {"contexts", "encode"}
        else str(
            assets.ROOT
            / "tools/semantic/.venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
    )
    proof = supervise(
        ["-m", "scripts.semantic_population_probe", *sys.argv[1:], "--child"],
        args.log,
        interpreter=interpreter,
    )
    if (
        not proof["completed"]
        or proof["seconds"] > policy.MAX_SECONDS
        or proof["peak_child_rss_bytes"] > policy.MAX_RSS
    ):
        raise SemanticError("POPULATION_FINAL_SUPERVISION_LIMIT")
    answer = None
    for line in reversed(args.log.read_bytes().splitlines()):
        if line.startswith(b'{"directory":'):
            answer = json.loads(line.decode("ascii"))
            break
    if answer is None:
        raise SemanticError("POPULATION_RESULT_MISSING")
    manifest = artifacts.verify(Path(answer["directory"]))
    result.parent.mkdir(parents=True, exist_ok=True)
    assets.write_json(
        result, answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(answer | {"supervision": proof}))


if __name__ == "__main__":
    main()
