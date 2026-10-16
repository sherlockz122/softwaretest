"""Manage the first pinned public CSV asset locally; no application API or training."""

import argparse
import json
from pathlib import Path

from packages.mining.public_assets import (
    ROOT,
    AssetError,
    catalog,
    import_asset,
    load_asset,
    page_asset,
    safe_path,
)
from scripts.fetch_public_dataset import verify_manifest

DEFAULT_STORE = ROOT / "runtime/public-assets"


def verified_asset(directory):
    value = load_asset(directory)
    source = verify_manifest(safe_path(Path(directory) / "source"))
    if source != value["manifest"]["identity"]["source_acquisition"]:
        raise AssetError("ASSET_SOURCE_CONFLICT")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    sub = parser.add_subparsers(dest="command", required=True)
    imported = sub.add_parser("import")
    imported.add_argument(
        "--published", type=Path, default=ROOT / "runtime/public-data/apachejit-v1"
    )
    sub.add_parser("list")
    shown = sub.add_parser("show")
    shown.add_argument("asset_id")
    page = sub.add_parser("page")
    page.add_argument("asset_id")
    page.add_argument("--offset", type=int, default=0)
    page.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    try:
        if args.command == "import":
            value = import_asset(args.published, args.store, verify_source=verify_manifest)
            result = {
                "asset_id": value["manifest"]["asset_id"],
                "rows": value["quality"]["rows"],
                "capabilities": value["quality"]["capabilities"],
            }
        elif args.command == "list":
            result = [
                {"asset_id": a["manifest"]["asset_id"], "rows": a["quality"]["rows"]}
                for a in catalog(args.store)
            ]
        else:
            from packages.mining.asset_reviews import hex_id

            directory = safe_path(args.store / hex_id(args.asset_id))
            value = verified_asset(directory)
            result = (
                value
                if args.command == "show"
                else page_asset(directory, offset=args.offset, limit=args.limit)
            )
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, (str(exc) if isinstance(exc, AssetError) else "ASSET_SOURCE_INVALID") + "\n")


if __name__ == "__main__":
    main()
