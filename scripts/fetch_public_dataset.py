"""Acquire pinned public research inputs; never execute upstream replication code."""

import argparse
import hashlib
import json
import shutil
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from packages.mining.public_dataset import fingerprint

REVISION = "b0633db37c5c54915f0c7f40286afc0d12743bfd"
ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    (
        "apachejit_total.csv",
        "https://zenodo.org/api/records/5907002/files/apachejit_total.csv/content",
        16467692,
        "md5",
        "98e102741020b3b13798d1e10a350689",
    ),
    (
        "apachejit_date.csv",
        f"https://raw.githubusercontent.com/Verssae/NeuroJIT/{REVISION}/"
        "data/dataset/apachejit_date.csv",
        10110738,
        "git-blob",
        "98184be0d41f8c22b576424f36c77f22a0f43262",
    ),
    (
        "neurojit-LICENSE.txt",
        f"https://raw.githubusercontent.com/Verssae/NeuroJIT/{REVISION}/LICENSE",
        10783,
        "git-blob",
        "0b46a61a7902e7b9bf28cb4c68dabaf43c383a81",
    ),
    (
        "neurojit-data_utils.py.txt",
        f"https://raw.githubusercontent.com/Verssae/NeuroJIT/{REVISION}/scripts/data_utils.py",
        6932,
        "git-blob",
        "baa92ee3b9b70a88128eb4c5665760cf3ea050c3",
    ),
)


def validate_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"zenodo.org", "raw.githubusercontent.com"}
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
    ):
        raise ValueError("Unapproved public source URL")


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def runtime_path(path):
    runtime = (ROOT / "runtime").resolve()
    resolved = path.resolve()
    if not runtime.is_relative_to(ROOT.resolve()) or not resolved.is_relative_to(runtime):
        raise ValueError("Public inputs must remain under project runtime")
    return resolved


def safe_file(directory, name):
    path = directory / name
    if path.is_symlink() or not path.resolve().is_relative_to(directory):
        raise ValueError("Public input file cannot follow links")
    return path


def checksums(path, size, kind, expected):
    if path.stat().st_size != size:
        raise ValueError(f"Source integrity mismatch: {path.name}")
    digest = hashlib.md5() if kind == "md5" else hashlib.sha1()
    if kind == "git-blob":
        digest.update(f"blob {size}\0".encode())
    sha256 = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if count + len(chunk) > size:
                raise ValueError(f"Source integrity mismatch: {path.name}")
            digest.update(chunk)
            sha256.update(chunk)
            count += len(chunk)
    if count != size or digest.hexdigest() != expected:
        raise ValueError(f"Source integrity mismatch: {path.name}")
    return sha256.hexdigest()


def verify_metadata(metadata):
    total = next((item for item in metadata["files"] if item["key"] == SOURCES[0][0]), None)
    if (
        metadata["id"] != 5907002
        or metadata["metadata"]["license"]["id"] != "cc-by-4.0"
        or total is None
        or total["size"] != SOURCES[0][2]
        or total["checksum"] != "md5:" + SOURCES[0][4]
    ):
        raise ValueError("Public release metadata mismatch")


def verify_manifest(directory):
    manifest_path = safe_file(directory, "acquisition.json")
    metadata_path = safe_file(directory, "zenodo-record.json")
    if metadata_path.stat().st_size > 512 * 1024 or manifest_path.stat().st_size > 64 * 1024:
        raise ValueError("Public metadata size exceeded")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_metadata(json.loads(metadata_path.read_text(encoding="utf-8")))
    files = []
    for name, url, size, kind, expected in SOURCES:
        files.append(
            {
                "name": name,
                "url": url,
                "bytes": size,
                "sha256": checksums(safe_file(directory, name), size, kind, expected),
            }
        )
    acquired = datetime.fromisoformat(manifest["acquired_at"])
    if (
        manifest["files"] != files
        or manifest["zenodo_record"] != 5907002
        or manifest["neurojit_revision"] != REVISION
        or manifest["apachejit_license"] != "CC-BY-4.0"
        or manifest["license_source"] != "https://zenodo.org/api/records/5907002"
        or manifest["license_metadata_sha256"] != fingerprint(metadata_path)
        or manifest["upstream_code_executed"] is not False
        or acquired.tzinfo is None
        or not datetime(2022, 1, 1, tzinfo=UTC) <= acquired <= datetime.now(UTC)
    ):
        raise ValueError("Acquisition manifest mismatch")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "runtime/public-data/apachejit-v1")
    args = parser.parse_args()
    directory = runtime_path(args.directory)
    required = sum(row[2] for row in SOURCES) * 2 + 2 * 1024**3
    if shutil.disk_usage(ROOT).free < required:
        raise ValueError("Insufficient project disk space; stop, never fall back to C")
    directory.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(PublicRedirect())
    metadata_path = safe_file(directory, "zenodo-record.json")
    if not metadata_path.exists():
        with opener.open("https://zenodo.org/api/records/5907002", timeout=45) as response:
            raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError("Public metadata size exceeded")
        metadata = json.loads(raw)
        verify_metadata(metadata)
        metadata_path.write_bytes(raw)
    verify_metadata(json.loads(metadata_path.read_text(encoding="utf-8")))
    files = []
    for name, url, size, kind, expected in SOURCES:
        validate_url(url)
        target = safe_file(directory, name)
        if not target.exists():
            partial = safe_file(directory, name + ".part")
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "DefectGuard-audit"})
                with opener.open(request, timeout=45) as response, partial.open("wb") as stream:
                    count = 0
                    while chunk := response.read(1024 * 1024):
                        count += len(chunk)
                        if count > size or shutil.disk_usage(ROOT).free < 2 * 1024**3:
                            raise ValueError("Source size or project capacity limit exceeded")
                        stream.write(chunk)
                checksums(partial, size, kind, expected)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        sha256 = checksums(target, size, kind, expected)
        files.append({"name": name, "url": url, "bytes": size, "sha256": sha256})
    manifest = {
        "acquired_at": datetime.now(UTC).isoformat(),
        "zenodo_record": 5907002,
        "apachejit_license": "CC-BY-4.0",
        "license_source": "https://zenodo.org/api/records/5907002",
        "license_metadata_sha256": fingerprint(metadata_path),
        "neurojit_revision": REVISION,
        "neurojit_package_license": "Apache-2.0; upstream dataset remains separately attributed",
        "files": files,
        "upstream_code_executed": False,
    }
    path = safe_file(directory, "acquisition.json")
    if not path.exists():
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify_manifest(directory)
    print(json.dumps({"directory": str(directory), "files_verified": len(files)}))


if __name__ == "__main__":
    main()
