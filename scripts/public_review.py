"""Record local review declarations; operator names are not application authentication."""

import argparse
import json
from pathlib import Path

from packages.mining.asset_reviews import append_review, case_history, initialize_reviews
from packages.mining.public_assets import ROOT, AssetError
from scripts.public_asset import verified_asset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    initial = sub.add_parser("init")
    initial.add_argument("--report", type=Path, required=True)
    initial.add_argument("--asset", type=Path, required=True)
    initial.add_argument("--store", type=Path, default=ROOT / "runtime/source-reviews")
    show = sub.add_parser("show")
    show.add_argument("directory", type=Path)
    show.add_argument("case_id")
    update = sub.add_parser("append")
    update.add_argument("directory", type=Path)
    update.add_argument("case_id")
    update.add_argument("--expected-revision", type=int, required=True)
    update.add_argument("--expected-hash", required=True)
    update.add_argument("--status", required=True)
    update.add_argument("--operator", required=True)
    update.add_argument("--rationale", required=True)
    update.add_argument("--evidence", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "init":
            verified_asset(args.asset)
            value = initialize_reviews(args.report, args.asset, args.store)
            result = {
                "context_id": value["context_id"],
                "cases": len(value["records"]),
                "readiness_promoted": False,
            }
        elif args.command == "show":
            result = case_history(args.directory, args.case_id)["records"]
        else:
            result = append_review(
                args.directory,
                args.case_id,
                expected_revision=args.expected_revision,
                expected_hash=args.expected_hash,
                status=args.status,
                operator=args.operator,
                rationale=args.rationale,
                evidence_paths=args.evidence,
            )
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, (str(exc) if isinstance(exc, AssetError) else "REVIEW_INPUT_INVALID") + "\n")


if __name__ == "__main__":
    main()
