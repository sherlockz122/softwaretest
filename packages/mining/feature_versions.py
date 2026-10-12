"""Online v2 bridge: native Git certification, minimal frozen inputs and shared statistics."""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from packages.mining import causal_features
from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import FEATURES, RULE_HASH, FeatureVector, digest, quality
from packages.repositories.safety import RepositoryError

V2 = causal_features.VERSION
SOURCE_VERSION = "native-record-binding-v1"


def rule_hash(version):
    if version == "kamei14-dg-v1":
        return RULE_HASH
    if version != V2:
        raise RepositoryError(422, "FEATURE_POLICY_UNSUPPORTED")
    return digest(
        {
            "causal": causal_features.rule_hash(),
            "online": Path(__file__).read_text(encoding="utf-8"),
        }
    )


def freeze_verified(reader, stored, encode):
    native = reader.record(stored["commit"]["sha"])
    # Certify all fields consumed from the parsed DB, including counts and the message.
    for key, value in stored["commit"].items():
        if native["commit"].get(key) != value:
            raise RepositoryError(409, "FEATURE_GIT_RECORD_CONFLICT")
    for key, value in stored["identity"].items():
        if native["identity"].get(key) != value:
            raise RepositoryError(409, "FEATURE_GIT_RECORD_CONFLICT")
    projected = (
        [{key: file[key] for key in stored["files"][0]} for file in native["files"]]
        if stored["files"]
        else native["files"]
    )
    if projected != stored["files"]:
        raise RepositoryError(409, "FEATURE_GIT_RECORD_CONFLICT")
    commit = freeze_native_record(reader, native)
    try:
        message = causal_features.freeze_message(native)
    except causal_features.CausalFeatureError:
        raise RepositoryError(422, "FEATURE_RESOURCE_LIMIT") from None
    return {
        "sha": commit.sha,
        "commit": encode(commit),
        "message_purpose": asdict(message),
        "native_record_digest": digest(native),
    }


def thaw(frozen, decode):
    try:
        inputs, messages = [], []
        for row in frozen:
            if set(row) != {"sha", "commit", "message_purpose", "native_record_digest"}:
                raise ValueError
            proof = row["native_record_digest"]
            if (
                not isinstance(proof, str)
                or len(proof) != 64
                or any(c not in "0123456789abcdef" for c in proof)
            ):
                raise ValueError
            commit = decode(row["commit"])
            message = causal_features.MessagePurpose(**row["message_purpose"])
            if row["sha"] != commit.sha or message.sha != commit.sha or commit.fix is not None:
                raise ValueError
            inputs.append(commit)
            messages.append(message)
        if len(json.dumps(frozen).encode()) > 32 * 1024**2:
            raise RepositoryError(422, "FEATURE_RESOURCE_LIMIT")
        return tuple(inputs), tuple(messages)
    except (KeyError, TypeError, ValueError):
        raise RepositoryError(409, "FEATURE_SOURCE_CONFLICT") from None


def calculate(snapshot, messages, frozen, tick):
    try:
        run = causal_features.compute_causal(snapshot, messages, tick=tick)
    except causal_features.CausalFeatureError as error:
        raise RepositoryError(
            422,
            "FEATURE_RESOURCE_LIMIT"
            if "LIMIT" in str(error) or "TIMEOUT" in str(error)
            else "FEATURE_SOURCE_CONFLICT",
        ) from None
    outputs = run.as_dict()["vectors"]
    # Reuse the exact v1 numeric quantile implementation without changing its contract.
    statistics = tuple(
        FeatureVector(
            row["sha"],
            datetime.fromisoformat(row["event_time"].removesuffix("Z")),
            row["source_digest"],
            tuple((key, row["values"][key]) for key in FEATURES),
            tuple(sorted(row["reasons"].items())),
        )
        for row in outputs
    )
    report = quality(statistics, tick) | {
        "feature_version": V2,
        "message_rule": causal_features.MESSAGE_RULE,
        "fix_semantics": (
            "message purpose indicator; 0 is no heuristic match, not confirmed non-fix or clean"
        ),
        "source_validation": {
            "version": SOURCE_VERSION,
            "accepted_head": snapshot.head_sha,
            "native_records_digest": digest([row["native_record_digest"] for row in frozen]),
            "feature_population_complete": snapshot.history_coverage == "full",
            "label_coverage_complete": False,
            "product_training_ready": False,
        },
    }
    return outputs, report
