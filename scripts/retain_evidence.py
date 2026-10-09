"""Create a safe, hash-indexed local acceptance manifest without credentials."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    root = Path(__file__).resolve().parents[1]
    if not directory.is_relative_to(root / "runtime/acceptance"):
        raise RuntimeError("Evidence must stay under project runtime/acceptance")
    values = []
    for config in [root / ".env", *sorted((root / "runtime/fresh").glob("*/.env"))]:
        if config.exists():
            for line in config.read_text(encoding="utf-8-sig").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    if ("PASSWORD" in key or "SIGNING_KEY" in key) and value:
                        values.append(value.encode())
    inventory = []
    for file in sorted(directory.rglob("*")):
        if not file.is_file() or file.name == "manifest.json":
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(directory):
            raise RuntimeError("Evidence cannot follow links outside its directory")
        if file.suffix in {".zip", ".har"} or "storage-state" in file.name or file.name == ".env":
            raise RuntimeError("Credential-bearing capture is not allowed in retained evidence")
        content = file.read_bytes()
        if file.suffix != ".png":
            for value in values:
                content = content.replace(value, b"[REDACTED]")
            content = re.sub(
                rb"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", b"[REDACTED_JWT]", content
            )
            content = re.sub(rb"(?i)Bearer\s+[A-Za-z0-9._-]{16,}", b"Bearer [REDACTED]", content)
            content = re.sub(
                rb"""(?i)((?:csrf_token|x-csrf-token|dg_refresh)[\\"':=\s]+)[A-Za-z0-9_-]{16,}""",
                rb"\g<1>[REDACTED]",
                content,
            )
            file.write_bytes(content)
        inventory.append(
            {
                "path": file.relative_to(directory).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    manifest = {
        "label": args.label,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "base_commit": sha,
        "source_state": "working tree at acceptance; final SHA in phase report/chat",
        "capture_policy": "local only; no trace/HAR/storageState; passwords masked",
        "files": inventory,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"PASS indexed {len(inventory)} local evidence files after credential redaction")


if __name__ == "__main__":
    main()
