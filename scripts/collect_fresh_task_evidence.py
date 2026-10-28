"""Bounded allowlisted DB summary before disposable environment cleanup."""

import argparse
import re
import subprocess
import sys
import threading
from pathlib import Path

from packages.mining import public_assets as assets
from packages.platform import acceptance_evidence as evidence


def bounded(command, *, timeout=15):
    body = bytearray()
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )

    def read():
        try:
            body.extend(process.stdout.read(evidence.MAX_BYTES + 1))
            if len(body) > evidence.MAX_BYTES:
                process.kill()
        finally:
            process.stdout.close()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
        reader.join(timeout=3)
        raise ValueError("TASK_EVIDENCE_TIMEOUT") from None
    reader.join(timeout=3)
    if reader.is_alive() or len(body) > evidence.MAX_BYTES:
        raise ValueError("TASK_EVIDENCE_OUTPUT_LIMIT")
    if process.returncode:
        raise ValueError("TASK_EVIDENCE_CHILD_FAILED")
    return evidence.decode(bytes(body))


def capture(command, output, *, runner=bounded):
    # Never retain command arguments, child stderr, messages or malformed stdout.
    try:
        value = evidence.validate(runner(command))
        result = {"collection_status": "succeeded", **value}
    except Exception:
        result = {
            "version": evidence.VERSION,
            "collection_status": "failed",
            "reason": "TASK_EVIDENCE_UNAVAILABLE",
            "tasks": [],
        }
    assets.atomic_json(output, result, check=lambda: None)
    return result["collection_status"] == "succeeded"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker")
    parser.add_argument("--project")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--override", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.project or not re.fullmatch(r"dg-fresh-[0-9a-f]{12}", args.project):
        parser.error("disposable project required")
    if not args.docker or not Path(args.docker).is_absolute():
        parser.error("absolute docker executable required")
    for path, parent in [
        (args.config, root / "runtime/fresh"),
        (args.override, root / "runtime/fresh"),
        (args.output, root / "runtime/acceptance"),
    ]:
        if path is None or not path.resolve().is_relative_to(parent.resolve()):
            parser.error("paths must stay in their local project runtime directories")
        assets.safe_path(path)
    # The app image contains packages, not developer scripts. No shell or credentials.
    code = (
        "import json; from packages.platform.acceptance_evidence import collect_database; "
        "print(json.dumps(collect_database(),ensure_ascii=True))"
    )
    command = [
        args.docker,
        "compose",
        "--project-name",
        args.project,
        "--env-file",
        str(args.config),
        "-f",
        str(root / "compose.yaml"),
        "-f",
        str(args.override),
        "exec",
        "-T",
        "api",
        "python",
        "-B",
        "-c",
        code,
    ]
    return 0 if capture(command, args.output) else 1


if __name__ == "__main__":
    sys.exit(main())
