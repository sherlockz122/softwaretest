"""Download only pinned official CodeBERT files and preserve provenance; never load bin."""

import hashlib
import json
import os
import shutil
import time
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.semantic.artifacts import file_digest

REVISION = "3b0952feddeffad0063f274080e3c23d75e7eb39"
FILES = {
    "pytorch_model.bin": (
        498627950,
        "sha256",
        "28b61fd8fa069f6bc966f4cb9572a4026ab2a784fca8fb224020d91b744e32d6",
    ),
    "config.json": (498, "git", "cd6f88de92758ae36c1c6e18a03ad288ccb41172"),
    "merges.txt": (456318, "git", "226b0752cac7789c48f0cb3ec53eda48b7be36cc"),
    "special_tokens_map.json": (150, "git", "6cd1d9021e10d47aed59399af6b0e30312b46ca4"),
    "tokenizer_config.json": (25, "git", "34ddbd64a4cd3f2d9d8a9120d3662d0bf91baead"),
    "vocab.json": (898822, "git", "0a39732b2d8be8e493cab3da68b68cc3e28221de"),
    "README.md": (1222, "git", "4ae9f25fc2572ef699d2e6b8a32c3ae6647c02f2"),
}


def acquire():
    root = assets.safe_path(assets.ROOT / "runtime/semantic-checkpoints")
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("codebert-raw-" + REVISION)
    if target.exists():
        receipt = assets.read_json(target / "acquisition.json", lambda: None)
        assets.verify_seal(receipt)
        for name, record in receipt["files"].items():
            if file_digest(target / name, lambda: None) != record:
                raise ValueError("SEMANTIC_WEIGHT_DRIFT")
        return target
    attempt = assets.safe_path(root / (".download-" + uuid4().hex))
    attempt.mkdir()
    started, records = time.monotonic(), {}
    for name, (size, kind, expected) in FILES.items():
        reused = False
        for previous in sorted(root.glob(".download-*"))[:5]:
            candidate = assets.safe_path(previous / name)
            if previous == attempt or not candidate.is_file() or candidate.stat().st_size != size:
                continue
            check = (
                hashlib.sha256() if kind == "sha256" else hashlib.sha1(f"blob {size}\0".encode())
            )
            with candidate.open("rb") as source:
                while block := source.read(1024**2):
                    check.update(block)
            if check.hexdigest() == expected:
                os.link(candidate, attempt / name)
                records[name] = file_digest(attempt / name)
                reused = True
                print(json.dumps({"file": name, "reused_verified_bytes": size}), flush=True)
                break
        if reused:
            continue
        url = f"https://huggingface.co/microsoft/codebert-base/resolve/{REVISION}/{name}"
        request = urllib.request.Request(url, headers={"User-Agent": "DefectGuard-research/1"})
        digest = hashlib.sha256() if kind == "sha256" else hashlib.sha1(f"blob {size}\0".encode())
        total = 0
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            (attempt / name).open("xb") as stream,
        ):
            while block := response.read(1024**2):
                total += len(block)
                if (
                    total > size
                    or time.monotonic() - started > 1200
                    or shutil.disk_usage(root).free < 2.5 * 1024**3
                ):
                    raise ValueError("SEMANTIC_DOWNLOAD_LIMIT")
                digest.update(block)
                stream.write(block)
                if total % (64 * 1024**2) < 1024**2:
                    print(json.dumps({"file": name, "downloaded_bytes": total}), flush=True)
        if total != size or digest.hexdigest() != expected:
            raise ValueError("SEMANTIC_WEIGHT_DRIFT")
        records[name] = file_digest(attempt / name, lambda: None)
    # Record the official repository license separately; model card has no license metadata.
    with urllib.request.urlopen(
        "https://api.github.com/repos/microsoft/CodeBERT/commits/master", timeout=30
    ) as response:
        license_revision = json.loads(response.read(1024**2))["sha"]
    license_url = f"https://raw.githubusercontent.com/microsoft/CodeBERT/{license_revision}/LICENSE"
    with urllib.request.urlopen(license_url, timeout=30) as response:
        license_text = response.read(16385)
    if len(license_text) > 16384 or b"MIT License" not in license_text:
        raise ValueError("SEMANTIC_LICENSE_INVALID")
    (attempt / "upstream-LICENSE").write_bytes(license_text)
    records["upstream-LICENSE"] = file_digest(attempt / "upstream-LICENSE", lambda: None)
    receipt = assets.sealed(
        {
            "version": "codebert-official-acquisition-v1",
            "model": "microsoft/codebert-base",
            "revision": REVISION,
            "acquired_at": datetime.now(UTC).isoformat(),
            "files": records,
            "official_repository_license_url": license_url,
            "repository_license": "MIT",
            "model_card_license_metadata": None,
            "redistribution_approved": False,
            "usage": "local research pilot; no redistribution or production release",
            "remote_code_executed": False,
        }
    )
    assets.write_json(attempt / "acquisition.json", receipt)
    attempt.rename(target)
    return target


if __name__ == "__main__":
    print(json.dumps({"directory": str(acquire()), "loaded_bin": False}))
