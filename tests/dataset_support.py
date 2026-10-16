"""Inert engineering CSVs; no real acquisition, label quality, or model evidence."""

import csv
import json
from datetime import UTC, datetime

from packages.mining import public_assets as assets
from packages.mining.public_dataset import DATE_FIELDS, NUMERIC_FIELDS, TOTAL_FIELDS
from packages.mining.source_evidence import canonical


def fixture_asset(root, *, rows=40, projects=("apache/activemq",), mutate=None, store=None):
    published = root / "published"
    published.mkdir(parents=True)
    total, dates = [], []
    for project in projects:
        for i in range(1, rows + 1):
            event, buggy, sha = 1000 * i, i % 2 == 0, f"{i:040x}"
            row = dict(
                zip(
                    TOTAL_FIELDS,
                    (
                        sha,
                        project,
                        str(buggy),
                        "False",
                        "2003",
                        str(event),
                        *(str(i) for _ in NUMERIC_FIELDS),
                    ),
                    strict=True,
                )
            )
            if mutate:
                mutate(row, i, project)
            total.append(row)
            actual_event = int(row["author_date"])
            actual_buggy = row["buggy"] == "True"
            dates.append(
                dict(
                    zip(
                        DATE_FIELDS,
                        (
                            sha,
                            project,
                            datetime.fromtimestamp(actual_event, UTC).isoformat()
                            if actual_buggy
                            else "",
                            datetime.fromtimestamp(actual_event + 100, UTC).isoformat()
                            if actual_buggy
                            else "",
                            row["buggy"],
                            row["fix"],
                            row["author_date"],
                            "2003",
                        ),
                        strict=True,
                    )
                )
            )
    for name, fields, records in (
        ("apachejit_total.csv", TOTAL_FIELDS, total),
        ("apachejit_date.csv", DATE_FIELDS, dates),
    ):
        with (published / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
    receipt = {
        "acquired_at": "2022-01-01T00:00:00+00:00",
        "fixture_not_real_acquisition": True,
        "apachejit_license": "CC-BY-4.0",
        "upstream_code_executed": False,
    }
    (published / "acquisition.json").write_bytes(canonical(receipt))
    for name in assets.SOURCE_NAMES - {
        "apachejit_total.csv",
        "apachejit_date.csv",
        "acquisition.json",
    }:
        (published / name).write_text("synthetic inert engineering fixture", encoding="utf-8")
    store = store or root / "store"
    imported = assets.import_asset(published, store, verify_source=lambda _: receipt)
    directory = store / imported["manifest"]["asset_id"]
    return directory, imported


def reseal_asset(directory):
    """Simulate self-consistent tampering; derived-vs-original checks must still reject."""
    manifest = json.loads((directory / "manifest.json").read_text())
    for record in manifest["identity"]["files"]:
        record.update(assets.file_digest(directory / record["path"], lambda: None))
    from packages.mining.source_evidence import content_hash

    manifest["asset_id"] = content_hash(manifest["identity"])
    manifest.pop("content_hash")
    manifest = assets.sealed(manifest)
    target = directory.with_name(manifest["asset_id"])
    directory.rename(target)
    (target / "manifest.json").write_bytes(canonical(manifest) + b"\n")
    return target, manifest
