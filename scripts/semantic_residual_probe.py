"""Supervised fixed residual diagnosis, without model forward or qualification."""

import argparse
import json
import os
import sys
from pathlib import Path

from packages.mining import public_assets as assets
from packages.semantic import artifacts, policy
from packages.semantic.control import supervise
from packages.semantic.policy import SemanticError
from scripts.semantic_coverage_probe import read_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("declare", "diagnose"))
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--declaration", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.phase == "diagnose" and any(
        p is None for p in (args.declaration, args.source, args.checkpoint)
    ):
        parser.error("diagnose requires declaration/source/checkpoint")
    if args.child:
        from packages.semantic import diagnostics

        target = (
            diagnostics.declare(args.inputs, args.output)
            if args.phase == "declare"
            else diagnostics.diagnose(
                args.inputs, args.declaration, args.source, args.checkpoint, args.output
            )
        )
        print(json.dumps({"directory": str(target)}), flush=True)
        return
    result = assets.safe_path(args.result)
    if result.exists():
        raise SemanticError("DIAGNOSIS_RESULT_EXISTS")
    interpreter = (
        sys.executable
        if args.phase == "declare"
        else str(
            assets.ROOT
            / "tools/semantic/.venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
    )
    proof = supervise(
        ["-m", "scripts.semantic_residual_probe", *sys.argv[1:], "--child"],
        args.log,
        interpreter=interpreter,
    )
    if proof["seconds"] > policy.MAX_SECONDS or proof["peak_child_rss_bytes"] > policy.MAX_RSS:
        raise SemanticError("DIAGNOSIS_FINAL_LIMIT")
    answer = read_result(args.log)
    manifest = artifacts.verify(Path(answer["directory"]))
    assets.write_json(
        result, answer | {"manifest_hash": manifest["content_hash"], "supervision": proof}
    )
    print(json.dumps(answer | {"supervision": proof}))


if __name__ == "__main__":
    main()
