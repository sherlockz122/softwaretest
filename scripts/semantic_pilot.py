"""Explicit offline research phases; expensive work runs in bounded child processes."""

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.semantic import artifacts
from packages.semantic.control import checkpoint, supervise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["extract", "convert", "encode", "study"])
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        checkpoint()
        if args.phase == "extract":
            from packages.semantic.git_inputs import extract

            target = extract(args.inputs, args.source, args.output, checkpoint)
        elif args.phase == "convert":
            from packages.semantic.weights import convert

            target = convert(args.inputs, args.output)
        elif args.phase == "encode":
            from packages.semantic.encoder import encode

            target = encode(args.inputs, args.checkpoint, args.output)
        else:
            from packages.semantic.study import study

            target = study(args.inputs, args.checkpoint, args.output)
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    result = assets.safe_path(args.result)
    if result.exists():
        raise ValueError("SEMANTIC_RESULT_EXISTS")
    interpreter = sys.executable
    if args.phase != "extract":
        interpreter = str(
            assets.ROOT
            / "tools/semantic/.venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
    log = args.log or assets.ROOT / "runtime/semantic-operations" / (
        args.phase + "-" + uuid4().hex + ".log"
    )
    proof = supervise(
        ["-m", "scripts.semantic_pilot", *sys.argv[1:], "--child"], log, interpreter=interpreter
    )
    lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
    answer = next(json.loads(line) for line in reversed(lines) if line.startswith('{"directory":'))
    manifest = artifacts.verify(Path(answer["directory"]))
    result.parent.mkdir(parents=True, exist_ok=True)
    assets.write_json(
        result, answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(answer | {"supervision": proof}), flush=True)


if __name__ == "__main__":
    main()
