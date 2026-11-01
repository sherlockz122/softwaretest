"""Explicit offline import/verification for the two approved fixed Git histories."""

import argparse
import json
from pathlib import Path

from packages.mining.public_assets import AssetError
from packages.repositories.safety import RepositoryError
from packages.semantic.offline_source import admit, safe_code, verify
from packages.semantic.policy import SemanticError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("offline-admit", "offline-verify"))
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.phase == "offline-admit":
        if args.output is None or args.source is not None:
            parser.error("offline-admit requires --output and forbids --source")
        target = admit(args.archive, args.output)
        print(json.dumps({"directory": str(target), "training_ready": False}))
    else:
        if args.source is None or args.output is not None:
            parser.error("offline-verify requires --source and forbids --output")
        receipt = verify(args.archive, args.source)
        print(json.dumps({"receipt_hash": receipt["content_hash"], "training_ready": False}))


if __name__ == "__main__":
    try:
        main()
    except (AssetError, SemanticError, RepositoryError) as error:
        code = safe_code(error)
        print(json.dumps({"success": False, "code": code}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"success": False, "code": "OFFLINE_ADMISSION_FAILED"}))
        raise SystemExit(1) from None
