"""Synthetic structural receipts test the validator, never claim upstream provenance."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.mining import public_assets as assets
from packages.semantic import artifacts
from packages.semantic import evaluation_weights as weights
from packages.semantic.policy import SemanticError


@pytest.fixture
def directory():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="evaluation-weights-") as name:
        yield Path(name)


def metadata_source(directory, monkeypatch):
    """Only hashes are fixture pins; model/revision/shape/LFS policy stays real."""
    source = directory / "metadata"
    source.mkdir()
    bodies = {
        "README.md": b"---\nlicense: apache-2.0\n---\nSynthetic test card.\n",
        "config.json": json.dumps(
            {
                "model_type": "roberta",
                "hidden_size": 768,
                "num_hidden_layers": 12,
                "vocab_size": 51416,
                "max_position_embeddings": 1026,
                "type_vocab_size": 10,
            }
        ).encode(),
        "tokenizer_config.json": b'{"tokenizer_class":"RobertaTokenizer"}',
        "special_tokens_map.json": b'{"cls_token":"<s>"}',
    }
    pins = {name: hashlib.sha256(raw).hexdigest() for name, raw in bodies.items()}
    monkeypatch.setattr(weights, "PINNED", pins)
    siblings, records = [], []
    for name, raw in bodies.items():
        (source / name).write_bytes(raw)
        oid = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        siblings.append({"rfilename": name, "blobId": oid, "size": len(raw)})
        records.append(
            {
                "name": name,
                "bytes": len(raw),
                "sha256": pins[name],
                "git_blob": oid,
                "observed_at_utc": "2026-01-01T00:00:00+00:00",
                "url": f"https://huggingface.co/{weights.MODEL}/resolve/{weights.REVISION}/{name}",
            }
        )
    binary = {
        "rfilename": "pytorch_model.bin",
        "size": weights.WEIGHT["bytes"],
        "lfs": {"size": weights.WEIGHT["bytes"], "sha256": weights.WEIGHT["sha256"]},
    }
    siblings.append(binary)
    assets.write_json(
        source / "metadata-with-blobs.json",
        {
            "id": weights.MODEL,
            "sha": weights.REVISION,
            "cardData": {"license": "apache-2.0"},
            "siblings": siblings,
        },
    )
    assets.write_json(
        source / "observation.json",
        {
            "model": weights.MODEL,
            "revision": weights.REVISION,
            "weights_downloaded": False,
            "remote_code_executed": False,
            "adopted": False,
            "qualification_promoted": False,
            "weight_metadata": binary,
            "files": records,
        },
    )
    return source


def rewrite(path, change):
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_fixed_receipts_preserve_original_time_without_qualification(directory, monkeypatch):
    source = metadata_source(directory, monkeypatch)
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    target = weights.register(source, directory / "registered")
    manifest = artifacts.verify(target)
    evidence = manifest["evidence"]
    assert manifest["version"] == weights.VERSION
    assert {name: (target / name).read_bytes() for name in original} == original
    assert (
        evidence["original_file_observations"]["README.md"]["observed_at_utc"]
        == "2026-01-01T00:00:00+00:00"
    )
    assert evidence["new_representation_protocol_required"] is True
    for field in (
        "training_ready",
        "candidate_release",
        "weights_downloaded",
        "safe_conversion_verified",
        "contamination_certified",
        "distribution_approved",
        "representation_compatible_with_old_CodeBERT",
    ):
        assert evidence[field] is False
    assert not (target / "pytorch_model.bin").exists()


@pytest.mark.parametrize(
    "case",
    [
        "revision",
        "model",
        "license",
        "lfs",
        "blob",
        "download",
        "time",
        "naive_time",
        "url",
        "duplicate",
        "extra",
        "missing",
        "bytes",
        "config",
        "card",
    ],
)
def test_invalid_or_overclaimed_evidence_refused(directory, monkeypatch, case):
    source = metadata_source(directory, monkeypatch)
    metadata, observation = source / "metadata-with-blobs.json", source / "observation.json"
    if case in {"revision", "model", "license"}:
        rewrite(
            metadata,
            lambda v: v.update(
                {"sha": "0" * 40}
                if case == "revision"
                else {"id": "other/model"}
                if case == "model"
                else {"cardData": {"license": "unknown"}}
            ),
        )
    elif case == "lfs":
        rewrite(metadata, lambda v: v["siblings"][-1]["lfs"].update(sha256="0" * 64))
    elif case == "blob":
        rewrite(metadata, lambda v: v["siblings"][0].update(blobId="0" * 40))
    elif case == "download":
        rewrite(observation, lambda v: v.update(weights_downloaded=True))
    elif case in {"time", "naive_time", "url"}:
        rewrite(
            observation,
            lambda v: v["files"][0].update(
                {"url": "https://example.invalid/model"}
                if case == "url"
                else {
                    "observed_at_utc": "2999-01-01T00:00:00+00:00"
                    if case == "time"
                    else "2026-01-01"
                }
            ),
        )
    elif case == "duplicate":
        rewrite(metadata, lambda v: v["siblings"].append(v["siblings"][0]))
    elif case == "extra":
        (source / "unexpected.bin").write_bytes(b"untrusted")
    elif case == "missing":
        (source / "README.md").unlink()
    else:
        name = "README.md" if case == "card" else "config.json"
        raw = b"No license frontmatter\n" if case == "card" else b'{"model_type":"bert"}'
        if case == "bytes":
            (source / name).write_bytes(raw)
        else:
            # A coherent new fixture hash cannot bypass the independent shape/license checks.
            (source / name).write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            monkeypatch.setitem(weights.PINNED, name, digest)
            oid = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            rewrite(
                metadata,
                lambda v: next(r for r in v["siblings"] if r["rfilename"] == name).update(
                    blobId=oid, size=len(raw)
                ),
            )
            rewrite(
                observation,
                lambda v: next(r for r in v["files"] if r["name"] == name).update(
                    git_blob=oid, sha256=digest, bytes=len(raw)
                ),
            )
    with pytest.raises(SemanticError):
        weights.register(source, directory / "registered")
    assert not list((directory / "registered").glob("*/manifest.json"))


def test_copy_drift_and_overlap_cannot_publish(directory, monkeypatch):
    source = metadata_source(directory, monkeypatch)
    with pytest.raises(SemanticError):
        weights.register(source, source / "nested")
    real_copy = weights.shutil.copyfile

    def corrupt(a, b):
        result = real_copy(a, b)
        Path(b).write_bytes(b"drift")
        return result

    monkeypatch.setattr(weights.shutil, "copyfile", corrupt)
    with pytest.raises(SemanticError):
        weights.register(source, directory / "registered")
    assert list((directory / "registered").glob(".weight-evidence-*"))
    assert not list((directory / "registered").glob("*/manifest.json"))
