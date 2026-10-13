"""Audit pinned public data locally; no network, database, fitting or model execution."""

import argparse
import json
from pathlib import Path

from packages.mining.public_dataset import PublicDataError, audit_paths
from scripts.fetch_public_dataset import ROOT, SOURCES, runtime_path, safe_file, verify_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "runtime/public-data/apachejit-v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory, output = runtime_path(args.directory), runtime_path(args.output)
    if output in [directory / source[0] for source in SOURCES] or output in (
        directory / "acquisition.json",
        directory / "zenodo-record.json",
    ):
        raise PublicDataError("PUBLIC_OUTPUT_SOURCE_COLLISION")
    manifest = verify_manifest(directory)
    report = audit_paths(directory / "apachejit_total.csv", directory / "apachejit_date.csv")
    if report["source_sha256"] != {
        "apachejit_total": manifest["files"][0]["sha256"],
        "neurojit_dates": manifest["files"][1]["sha256"],
    }:
        raise PublicDataError("PUBLIC_SOURCE_CHANGED")
    report["acquisition"] = manifest
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_file(output.parent, output.name + ".part")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "rows",
                    "project_groups",
                    "labels",
                    "date_quality",
                    "published_benchmark_structurally_ready",
                    "product_training_ready",
                    "content_hash",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
