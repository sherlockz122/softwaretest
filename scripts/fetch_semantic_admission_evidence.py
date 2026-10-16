"""Bounded official metadata observation; never infer a new license grant or contact others."""

import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from packages.mining import public_assets as assets
from packages.semantic import artifacts, policy


def observe(raw, output):
    raw, output = assets.safe_path(Path(raw)), assets.safe_path(Path(output))
    receipt = assets.read_json(raw / "acquisition.json", lambda: None)
    assets.verify_seal(receipt)
    if receipt["model"] != policy.MODEL or receipt["revision"] != policy.REVISION:
        raise ValueError("ADMISSION_MODEL_DRIFT")
    for name in ["README.md", "upstream-LICENSE"]:
        if artifacts.file_digest(raw / name) != receipt["files"][name]:
            raise ValueError("ADMISSION_LICENSE_BYTES_DRIFT")
    url = f"https://huggingface.co/api/models/{policy.MODEL}/revision/{policy.REVISION}"
    request = urllib.request.Request(url, headers={"User-Agent": "DefectGuard-admission"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(1024**2 + 1)
    if len(body) > 1024**2:
        raise ValueError("ADMISSION_METADATA_LIMIT")
    metadata = json.loads(body)
    if metadata["sha"] != policy.REVISION or metadata["id"] != policy.MODEL:
        raise ValueError("ADMISSION_METADATA_BINDING_DRIFT")
    value = assets.sealed(
        {
            "version": "semantic-license-observation-v1",
            "model": policy.MODEL,
            "revision": policy.REVISION,
            "observed_at": datetime.now(UTC).isoformat(),
            "metadata_url": url,
            "metadata_sha256": __import__("hashlib").sha256(body).hexdigest(),
            "model_card_license_metadata": (metadata.get("cardData") or {}).get("license"),
            "official_repository_license_url": receipt["official_repository_license_url"],
            "repository_license": receipt["repository_license"],
            "original_acquisition_hash": receipt["content_hash"],
            "original_acquired_at": receipt["acquired_at"],
            "redistribution_approved": False,
            "legal_prohibition_concluded": False,
            "reason": (
                "repository MIT recorded; fixed weight scope requires authoritative evidence; "
                "search absence is not proof of no license"
            ),
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    assets.write_json(output, value)
    print(
        json.dumps(
            {
                "observation": str(output),
                "content_hash": value["content_hash"],
                "card_license": value["model_card_license_metadata"],
            }
        )
    )


if __name__ == "__main__":
    import sys

    observe(*sys.argv[1:])
