"""Join fixed published cohorts to raw candidates; export an immutable review packet."""

import argparse
import json
import shutil
from pathlib import Path

from packages.mining.public_dataset import PublicDataError, audit_paths
from packages.mining.source_catalog import PROJECTS
from packages.mining.source_evidence import audit_sources, content_hash
from scripts.fetch_public_dataset import ROOT, verify_manifest
from scripts.fetch_source_evidence import publish_json, verify_bundle
from scripts.probe_causal_features import checked_path


def run(published, raw, output):
    published, raw, output = (checked_path(p) for p in (published, raw, output))
    if not output.is_relative_to(ROOT / "runtime/acceptance") or output.exists():
        raise PublicDataError("SOURCE_OUTPUT_INVALID")
    if output.is_relative_to(published) or output.is_relative_to(raw):
        raise PublicDataError("SOURCE_OUTPUT_COLLISION")
    release = verify_manifest(published)
    bundle = verify_bundle(raw)

    def tick():
        if shutil.disk_usage(ROOT).free < 2 * 1024**3:
            raise PublicDataError("SOURCE_CAPACITY_UNSAFE")

    public = audit_paths(published / "apachejit_total.csv", published / "apachejit_date.csv")
    if list(public["source_sha256"].values()) != [f["sha256"] for f in release["files"][:2]]:
        raise PublicDataError("SOURCE_PUBLISHED_CHANGED")
    result = audit_sources(
        published / "apachejit_date.csv",
        {code: raw / f"commit_links_{code}.csv" for code in PROJECTS},
        acquired_at=bundle["acquired_at"],
        tick=tick,
    ).as_dict()
    expected = {"published_dates": release["files"][1]["sha256"]}
    expected.update({code: bundle["files"][i]["sha256"] for i, code in enumerate(PROJECTS)})
    if result["source_sha256"] != expected or verify_bundle(raw) != bundle:
        raise PublicDataError("SOURCE_BUNDLE_CHANGED")
    if verify_manifest(published) != release:
        raise PublicDataError("SOURCE_PUBLISHED_CHANGED")
    result.update(
        raw_acquisition=bundle,
        published_acquisition=release,
        published_content_hash=content_hash(public),
        published_feature_version=public["audit_version"],
    )
    result.pop("content_hash")
    result["content_hash"] = content_hash(result)
    tick()
    publish_json(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published", type=Path, default=ROOT / "runtime/public-data/apachejit-v1")
    parser.add_argument("--raw", type=Path, default=ROOT / "runtime/public-data/apachejit-raw-v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.published, args.raw, args.output)
    print(
        json.dumps(
            {
                "version": result["version"],
                "rows": result["rows"],
                "input_rows": result["input_rows"],
                "projects": len(result["projects"]),
                "review_rows": len(result["review_packet"]),
                "product_training_ready": result["product_training_ready"],
                "content_hash": result["content_hash"],
            }
        )
    )


if __name__ == "__main__":
    main()
