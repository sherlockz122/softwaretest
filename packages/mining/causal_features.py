"""Independent message-purpose features; never consume later Fix review or labels."""

import hashlib
import json
import math
import time
from dataclasses import dataclass, replace
from pathlib import Path

from packages.mining import fix_rules
from packages.mining.features import FEATURES, RULE_HASH, SHA, Snapshot, compute, digest

VERSION = "kamei14-dg-message-v2"
MESSAGE_RULE = "commit-message-purpose-v1"


class CausalFeatureError(ValueError):
    pass


@dataclass(frozen=True)
class CausalRun:
    canonical_json: str

    def as_dict(self):
        return json.loads(self.canonical_json)

    @property
    def content_hash(self):
        return digest(self.as_dict())


def rule_hash():
    return digest(
        {
            "base_numeric_rule": RULE_HASH,
            "message_rule": fix_rules.RULE_HASH,
            "code": Path(__file__).read_text(encoding="utf-8"),
        }
    )


@dataclass(frozen=True)
class MessagePurpose:
    sha: str
    value: int | None
    message_digest: str
    reason: str

    def __post_init__(self):
        if (
            not isinstance(self.sha, str)
            or not SHA.fullmatch(self.sha)
            or self.value is not None
            and (type(self.value) is not int or self.value not in (0, 1))
            or not isinstance(self.message_digest, str)
            or len(self.message_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.message_digest)
            or self.reason not in ("message_purpose", "message_encoding", "message_unverified")
            or (self.value is None) != (self.reason != "message_purpose")
        ):
            raise CausalFeatureError("CAUSAL_MESSAGE_INVALID")


def freeze_message(record):
    """Caller must bind NativeReader's record to the accepted immutable Git SHA."""
    commit = record["commit"]
    message = commit["message"]
    if not isinstance(message, str) or len(message.encode("utf-8")) > 65536:
        raise CausalFeatureError("CAUSAL_MESSAGE_LIMIT")
    hashed = hashlib.sha256(message.encode("utf-8")).hexdigest()
    if commit["parse_status"] == "message_encoding":
        return MessagePurpose(commit["sha"], None, hashed, "message_encoding")
    # Legacy merge_skipped overrides the decoding status, so it cannot certify text.
    if commit["parse_status"] != "parsed":
        return MessagePurpose(commit["sha"], None, hashed, "message_unverified")
    result = fix_rules.evaluate(message, commit["parse_status"], record["files"], [])
    return MessagePurpose(commit["sha"], int(result["rule_candidate"]), hashed, "message_purpose")


def compute_causal(snapshot, messages, *, max_commits=1000, seconds=30, tick=lambda: None):
    """Keep numeric v1 formulas, replace FIX with the same causal input in both uses."""
    if (
        not isinstance(snapshot, Snapshot)
        or type(messages) is not tuple
        or any(not isinstance(x, MessagePurpose) for x in messages)
        or type(max_commits) is not int
        or not 1 <= max_commits <= 1000
        or type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 30
    ):
        raise CausalFeatureError("CAUSAL_POLICY_INVALID")
    if len(snapshot.commits) > max_commits:
        raise CausalFeatureError("CAUSAL_COMMIT_LIMIT")
    if sum(len(c.files) for c in snapshot.commits) > 10000:
        raise CausalFeatureError("CAUSAL_FILE_LIMIT")
    signals = {x.sha: x for x in messages}
    if len(signals) != len(messages) or set(signals) != {c.sha for c in snapshot.commits}:
        raise CausalFeatureError("CAUSAL_MESSAGE_POPULATION")
    started = time.perf_counter()

    def check():
        tick()
        if time.perf_counter() - started >= seconds:
            raise CausalFeatureError("CAUSAL_TIMEOUT")

    # Future review data is removed before hashing as well as before calculation.
    safe = replace(snapshot, commits=tuple(replace(c, fix=None) for c in snapshot.commits))
    base = compute(safe, tick=check)
    source = digest(
        {
            "base_source": base[0].source_digest,
            "version": VERSION,
            "rule_hash": rule_hash(),
            "messages": [vars(signals[c.sha]) for c in sorted(safe.commits, key=lambda c: c.sha)],
        }
    )
    output = []
    for vector in base:
        check()
        signal = signals[vector.sha]
        values, reasons = dict(vector.values), dict(vector.reasons)
        values["FIX"] = signal.value
        reasons.pop("FIX", None)
        if signal.value is None:
            reasons["FIX"] = signal.reason
        output.append(
            {
                "sha": vector.sha,
                "event_time": vector.event_time.isoformat() + "Z",
                "feature_version": VERSION,
                "source_digest": source,
                "message_rule": MESSAGE_RULE,
                "values": {key: values[key] for key in FEATURES},
                "reasons": reasons,
                "missing": [key for key in FEATURES if values[key] is None],
                "fix_semantics": "message purpose indicator, not a defect outcome or confirmation",
            }
        )
    return CausalRun(
        json.dumps(
            {
                "repository": snapshot.repository_id,
                "head_sha": snapshot.head_sha,
                "history_coverage": snapshot.history_coverage,
                "feature_version": VERSION,
                "rule_hash": rule_hash(),
                "vectors": output,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
