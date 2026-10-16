"""Acquire fixed raw SZZ links and filter notebooks, without executing upstream code."""

import argparse
import json
import os
import shutil
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from packages.mining.public_dataset import PublicDataError
from packages.mining.source_catalog import REPOSITORY, REVISION, SOURCES, source_url
from packages.mining.source_evidence import acquisition_time, canonical, content_hash
from scripts.fetch_public_dataset import ROOT, PublicRedirect, checksums
from scripts.probe_causal_features import checked_path

MANIFEST_VERSION = "apachejit-raw-acquisition-v1"


def publish_json(output, value):
    """Publish an entire same-directory file with exclusive link creation, never replace."""
    output = checked_path(output)
    if output.exists():
        raise PublicDataError("SOURCE_OUTPUT_EXISTS")
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_name(f".{output.name}.{uuid.uuid4().hex}.part")
    try:
        with part.open("xb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(part, output)
    finally:
        part.unlink(missing_ok=True)


def source_file(directory, relative):
    return checked_path(directory / Path(relative).name)


def file_records(directory):
    return [
        {
            "path": path,
            "url": source_url(path),
            "bytes": size,
            "git_blob": blob,
            "sha256": checksums(source_file(directory, path), size, "git-blob", blob),
        }
        for path, size, blob in SOURCES
    ]


def verify_bundle(directory):
    directory = checked_path(directory)
    path = checked_path(directory / "acquisition.json")
    if path.stat().st_size > 64 * 1024:
        raise PublicDataError("SOURCE_MANIFEST_LIMIT")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "version",
        "repository",
        "revision",
        "acquired_at",
        "acquisition_semantics",
        "files",
        "upstream_code_executed",
        "raw_source_license_verified",
        "manifest_hash",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise PublicDataError("SOURCE_MANIFEST_INVALID")
    acquisition_time(value["acquired_at"])
    if (
        value["version"] != MANIFEST_VERSION
        or value["repository"] != REPOSITORY
        or value["revision"] != REVISION
        or value["acquisition_semantics"] != "verified_bundle_capture_not_historical_knowledge"
        or value["upstream_code_executed"] is not False
        or value["raw_source_license_verified"] is not False
        or value["files"] != file_records(directory)
        or value["manifest_hash"]
        != content_hash({k: v for k, v in value.items() if k != "manifest_hash"})
    ):
        raise PublicDataError("SOURCE_MANIFEST_INVALID")
    return value


def acquire(directory):
    directory = checked_path(directory)
    if (directory / "acquisition.json").exists():
        return verify_bundle(directory)
    expected = sum(size for _, size, _ in SOURCES)
    if shutil.disk_usage(ROOT).free < 2 * 1024**3 + expected * 2:
        raise PublicDataError("SOURCE_CAPACITY_UNSAFE")
    directory.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(PublicRedirect())
    deadline = time.monotonic() + 1200
    for relative, size, blob in SOURCES:
        output = source_file(directory, relative)
        if output.exists():
            checksums(output, size, "git-blob", blob)
            continue
        part = source_file(directory, f"{output.name}.{uuid.uuid4().hex}.part")
        try:
            count = 0
            file_deadline = min(deadline, time.monotonic() + 120)
            request = urllib.request.Request(
                source_url(relative), headers={"User-Agent": "DefectGuard"}
            )
            with opener.open(request, timeout=45) as response, part.open("xb") as stream:
                while chunk := response.read(1024 * 1024):
                    if time.monotonic() > file_deadline:
                        raise PublicDataError("SOURCE_DOWNLOAD_DEADLINE")
                    count += len(chunk)
                    if count > size:
                        raise PublicDataError("SOURCE_DOWNLOAD_LIMIT")
                    if shutil.disk_usage(ROOT).free < 2 * 1024**3 + expected:
                        raise PublicDataError("SOURCE_CAPACITY_UNSAFE")
                    stream.write(chunk)
            checksums(part, size, "git-blob", blob)
            os.link(part, output)
        finally:
            part.unlink(missing_ok=True)
    value = {
        "version": MANIFEST_VERSION,
        "repository": REPOSITORY,
        "revision": REVISION,
        "acquired_at": datetime.now(UTC).isoformat(),
        "acquisition_semantics": "verified_bundle_capture_not_historical_knowledge",
        "files": file_records(directory),
        "upstream_code_executed": False,
        # Published Zenodo dataset license is recorded separately; do not extend it to raw code.
        "raw_source_license_verified": False,
    }
    value["manifest_hash"] = content_hash(value)
    publish_json(directory / "acquisition.json", value)
    return verify_bundle(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=ROOT / "runtime/public-data/apachejit-raw-v1"
    )
    args = parser.parse_args()
    value = acquire(args.directory)
    print(
        json.dumps(
            {
                "revision": value["revision"],
                "files": len(value["files"]),
                "acquired_at": value["acquired_at"],
                "manifest_hash": value["manifest_hash"],
            }
        )
    )


if __name__ == "__main__":
    main()
