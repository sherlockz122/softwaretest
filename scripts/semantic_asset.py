"""Install a hash-bound local research asset; no remote code or model execution."""

import argparse
import json
from pathlib import Path

from packages.mining import public_assets as assets
from packages.semantic import asset_catalog as catalog
from packages.semantic.asset_reader import ConsumptionError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-origin", required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    # Exclusive result must be available before doing a deployment operation.
    result = assets.safe_path(args.result)
    if result.exists():
        parser.error("result must not exist")
    result.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory = catalog.install(args.source, args.expected_origin)
        assets.write_json(
            result,
            {
                "status": "installed_restricted_research",
                "directory": str(directory),
                "asset_id": directory.name,
                "origin_artifact": args.expected_origin,
                "training_ready": False,
                "candidate_release": False,
            },
        )
        print(json.dumps({"asset_id": directory.name, "training_ready": False}))
    except ConsumptionError as exc:
        assets.write_json(result, {"status": "rejected", "code": exc.code})
        raise SystemExit(exc.code) from exc


if __name__ == "__main__":
    main()
