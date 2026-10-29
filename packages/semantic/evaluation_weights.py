"""Pinned official small-file evidence; never a weight loader or eligibility grant."""

import hashlib
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.semantic import artifacts
from packages.semantic.policy import SemanticError

VERSION = "native-evaluation-weight-evidence-v1"
MODEL = "microsoft/unixcoder-base"
REVISION = "5604afdc964f6c53782a6813140ade5216b99006"
WEIGHT = {
    "bytes": 503809527,
    "sha256": "6732b60037b086557e4c731c3748c99b4fec735d9ffe3c4c70061fd142e6b9d9",
}
PINNED = {
    "README.md": "4257702415ce5590e143f628b39dd434c94f911eccde62f723d8f97f32c41445",
    "config.json": "a7c07558348da020fb6055541fd593f21a6f0142a398da17bc452d50d3aef927",
    "tokenizer_config.json": "44b6e7a4fd703a619b6f8c297a8799f71ac14fdcb7f2d1ad8d8211649abed5e4",
    "special_tokens_map.json": "c611b1f7d416eb001ee4f293d903ea8c88e703463f1d403f1866a0352743fd00",
}


def require(condition, code="EVAL_WEIGHT_EVIDENCE_INVALID"):
    if not condition:
        raise SemanticError(code)


def checked(directory, check=lambda: None):
    directory = assets.safe_path(Path(directory))
    expected = {*PINNED, "metadata-with-blobs.json", "observation.json"}
    require({p.name for p in directory.iterdir()} == expected)
    metadata = assets.read_json(directory / "metadata-with-blobs.json", check, maximum=512 * 1024)
    observed = assets.read_json(directory / "observation.json", check, maximum=65536)
    require(
        metadata.get("sha") == REVISION
        and metadata.get("id", metadata.get("modelId")) == MODEL
        and metadata.get("cardData", {}).get("license") == "apache-2.0"
        and observed.get("model") == MODEL
        and observed.get("revision") == REVISION
        and observed.get("weights_downloaded") is False
        and observed.get("remote_code_executed") is False
        and observed.get("adopted") is False
        and observed.get("qualification_promoted") is False
    )
    siblings = metadata.get("siblings")
    require(isinstance(siblings, list) and len(siblings) <= 64)
    by_name = {r["rfilename"]: r for r in siblings}
    require(len(by_name) == len(siblings))
    binary = by_name.get("pytorch_model.bin", {})
    require(
        binary.get("size") == WEIGHT["bytes"]
        and binary.get("lfs", {}).get("size") == WEIGHT["bytes"]
        and binary.get("lfs", {}).get("sha256") == WEIGHT["sha256"]
        and observed.get("weight_metadata") == binary
    )
    records = observed.get("files")
    require(isinstance(records, list) and len(records) == len(PINNED))
    observations = {r["name"]: r for r in records}
    require(set(observations) == set(PINNED))
    originals = {}
    for name, digest in PINNED.items():
        check()
        record = observations[name]
        actual = assets.file_digest(directory / name, check, maximum=65536)
        require(
            actual["sha256"] == digest and actual == {k: record[k] for k in ("bytes", "sha256")}
        )
        raw = (directory / name).read_bytes()
        oid = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        sibling = by_name.get(name, {})
        require(
            not sibling.get("lfs")
            and sibling.get("blobId") == oid == record["git_blob"]
            and sibling.get("size") == len(raw)
            and record["url"] == f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}"
        )
        timestamp = datetime.fromisoformat(record["observed_at_utc"])
        require(timestamp.utcoffset() is not None and timestamp.utcoffset().total_seconds() == 0)
        require(timestamp <= datetime.now(UTC))
        originals[name] = record
    card = (directory / "README.md").read_text(encoding="utf-8")
    require(card.startswith("---\n"))
    frontmatter = card.split("---", 2)[1]
    require(re.search(r"(?m)^license:\s*apache-2\.0\s*$", frontmatter) is not None)
    config = assets.read_json(directory / "config.json", check, maximum=65536)
    require(
        {
            k: config.get(k)
            for k in (
                "model_type",
                "hidden_size",
                "num_hidden_layers",
                "vocab_size",
                "max_position_embeddings",
                "type_vocab_size",
            )
        }
        == {
            "model_type": "roberta",
            "hidden_size": 768,
            "num_hidden_layers": 12,
            "vocab_size": 51416,
            "max_position_embeddings": 1026,
            "type_vocab_size": 10,
        }
    )
    return {
        "model": MODEL,
        "revision": REVISION,
        "license_evidence": "official_fixed_model_card_explicit_apache-2.0",
        "original_file_observations": originals,
        "expected_weight": WEIGHT,
        "weights_downloaded": False,
        "safe_conversion_verified": False,
        "representation_compatible_with_old_CodeBERT": False,
        "new_representation_protocol_required": True,
        "contamination_certified": False,
        "distribution_approved": False,
        "training_ready": False,
        "candidate_release": False,
    }


def register(directory, output, check=lambda: None):
    directory, output = assets.safe_path(Path(directory)), assets.safe_path(Path(output))
    require(not output.is_relative_to(directory) and not directory.is_relative_to(output))
    source_names = [
        "packages/semantic/evaluation_weights.py",
        "packages/semantic/artifacts.py",
        "packages/mining/public_assets.py",
    ]
    execution = artifacts.sources(source_names)
    value = checked(directory, check)
    before = {
        p.name: artifacts.file_digest(p, check, maximum=512 * 1024) for p in directory.iterdir()
    }
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".weight-evidence-" + uuid4().hex)
    attempt.mkdir()
    for name in sorted(before):
        shutil.copyfile(directory / name, attempt / name)
        require(artifacts.file_digest(attempt / name, check, maximum=512 * 1024) == before[name])
    check()
    require(checked(directory, check) == value)
    require(
        before
        == {
            p.name: artifacts.file_digest(p, check, maximum=512 * 1024) for p in directory.iterdir()
        }
    )
    require(artifacts.sources(source_names) == execution, "EVAL_WEIGHT_EXECUTION_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION,
            "assembled_at": datetime.now(UTC).isoformat(),
            "evidence": value,
            "sources": execution,
        },
        sorted(before),
    )
