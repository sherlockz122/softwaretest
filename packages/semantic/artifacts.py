"""Dedicated large-model limits; never relax the public asset storage contract."""

import hashlib
import json
from pathlib import Path

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic.policy import SemanticError


def sources(names):
    return {
        name: hashlib.sha256((assets.ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for name in names
    }


def file_digest(path, check=lambda: None, maximum=1024**3):
    if type(maximum) is not int or not 0 < maximum <= 1024**3:
        raise SemanticError("SEMANTIC_FILE_LIMIT")
    return assets.file_digest(path, check, maximum=maximum)


def verify(directory, check=lambda: None):
    directory = assets.safe_path(Path(directory))
    manifest = assets.read_json(directory / "manifest.json", check, maximum=2 * 1024**2)
    assets.verify_seal(manifest)
    if directory.name != content_hash(manifest):
        raise SemanticError("SEMANTIC_MANIFEST_ID_DRIFT")
    for name, record in manifest["files"].items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in {".", ".."}
            or "\\" in name
        ):
            raise SemanticError("SEMANTIC_PATH_INVALID")
        if file_digest(directory / name, check) != record:
            raise SemanticError("SEMANTIC_ARTIFACT_DRIFT")
    return manifest


def publish(attempt, root, metadata, names):
    manifest = assets.sealed(metadata | {"files": {n: file_digest(attempt / n) for n in names}})
    assets.write_json(attempt / "manifest.json", manifest)
    target = assets.safe_path(root / content_hash(manifest))
    if target.exists():
        if verify(target) != manifest:
            raise SemanticError("SEMANTIC_PUBLICATION_CONFLICT")
        return target
    attempt.rename(target)
    return target


def records(directory):
    manifest = verify(directory)
    if type(manifest.get("accepted")) is not int or not 0 < manifest["accepted"] <= 2000:
        raise SemanticError("SEMANTIC_POPULATION_UNUSABLE")
    with (directory / "records.jsonl").open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index >= 2000:
                raise SemanticError("SEMANTIC_POPULATION_LIMIT")
            if len(line) > 4 * 1024**2:
                raise SemanticError("SEMANTIC_RECORD_LIMIT")
            yield json.loads(line)
