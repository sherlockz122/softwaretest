"""Bounded admission preparation, separate from the frozen stage23 CLI and artifacts."""

import argparse
import json
import os
import sys
from pathlib import Path

from packages.mining import public_assets as assets
from packages.semantic import artifacts
from packages.semantic.control import supervise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["contexts", "reserve", "review", "readiness"])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--license", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        from packages.semantic.admission import contexts, reserve

        if args.phase == "contexts":
            target = contexts(args.inputs, args.source, args.checkpoint, args.output)
        elif args.phase == "reserve":
            target = reserve(args.inputs, args.source, args.output)
        else:
            from packages.semantic.admission_review import readiness, review_bundle

            target = (
                review_bundle(args.inputs, args.source, args.output)
                if args.phase == "review"
                else readiness(
                    args.inputs,
                    args.source,
                    args.checkpoint,
                    args.reviews,
                    args.license,
                    args.output,
                )
            )
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    result = assets.safe_path(args.result)
    if result.exists():
        raise ValueError("ADMISSION_RESULT_EXISTS")
    interpreter = (
        assets.ROOT
        / "tools/semantic/.venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    proof = supervise(
        ["-m", "scripts.semantic_admission", *sys.argv[1:], "--child"],
        args.log,
        interpreter=str(interpreter),
    )
    answer = next(
        json.loads(line)
        for line in reversed(args.log.read_text(encoding="utf-8").splitlines())
        if line.startswith('{"directory":')
    )
    manifest = artifacts.verify(Path(answer["directory"]))
    result.parent.mkdir(parents=True, exist_ok=True)
    assets.write_json(
        result, answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(answer | {"supervision": proof}))


if __name__ == "__main__":
    main()
