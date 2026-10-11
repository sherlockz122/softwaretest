"""Immutable Kamei14 engineering kernel; no DB, Git mutation, labels or imputation."""

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from packages.repositories.parse_policy import IDENTITY_VERSION, PARSER_VERSION

FEATURE_VERSION = "kamei14-dg-v1"
FEATURES = (
    "NS",
    "ND",
    "NF",
    "Entropy",
    "LA",
    "LD",
    "LT",
    "FIX",
    "NDEV",
    "AGE",
    "NUC",
    "EXP",
    "REXP",
    "SEXP",
)
HISTORY = ("NDEV", "AGE", "NUC", "EXP", "REXP", "SEXP")
RULE_HASH = hashlib.sha256(
    b"\0".join(
        Path(__file__).with_name(name).read_text(encoding="utf-8").encode()
        for name in ("features.py", "feature_inputs.py")
    )
).hexdigest()
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class FeatureInputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat(timespec="microseconds") + "Z"
        raise TypeError

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode).encode()
    ).hexdigest()


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise FeatureInputError("FEATURE_UTC_REQUIRED")


@dataclass(frozen=True)
class FileDelta:
    old_path: str | None
    new_path: str | None
    change_type: str
    insertions: int | None
    deletions: int | None
    old_loc: int | None
    content_status: str = "parsed"
    is_binary: bool = False
    old_mode: str = "100644"
    new_mode: str = "100644"

    def __post_init__(self):
        for path in (self.old_path, self.new_path):
            if path is not None and (
                not isinstance(path, str)
                or not path
                or "\x00" in path
                or path.startswith("/")
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(0xD800 <= ord(char) <= 0xDFFF for char in path)
            ):
                raise FeatureInputError("FEATURE_PATH_INVALID")
        expected = {
            "A": self.old_path is None and self.new_path is not None,
            "D": self.new_path is None and self.old_path is not None,
            "M": self.old_path is not None and self.old_path == self.new_path,
            "R": self.old_path is not None and self.new_path is not None,
            "C": self.old_path is not None and self.new_path is not None,
            "T": self.old_path is not None and self.old_path == self.new_path,
        }
        if (
            not isinstance(self.change_type, str)
            or not expected.get(self.change_type)
            or not isinstance(self.content_status, str)
            or self.old_mode not in ("000000", "100644", "100755", "120000", "160000")
            or self.new_mode not in ("000000", "100644", "100755", "120000", "160000")
        ):
            raise FeatureInputError("FEATURE_FILE_INVALID")
        if type(self.is_binary) is not bool:
            raise FeatureInputError("FEATURE_FILE_INVALID")
        for value in (self.insertions, self.deletions, self.old_loc):
            if value is not None and (type(value) is not int or value < 0):
                raise FeatureInputError("FEATURE_COUNT_INVALID")

    @property
    def path(self):
        return self.new_path or self.old_path

    @property
    def supported(self):
        return (
            self.content_status == "parsed"
            and not self.is_binary
            and self.change_type != "T"
            and self.old_mode in ("000000", "100644", "100755")
            and self.new_mode in ("000000", "100644", "100755")
            and all(v is not None for v in (self.insertions, self.deletions, self.old_loc))
        )


@dataclass(frozen=True)
class FixSignal:
    value: bool
    available_at: datetime
    rule_version: str
    review_revision: int
    evidence_digest: str

    def __post_init__(self):
        utc(self.available_at)
        if (
            type(self.value) is not bool
            or not isinstance(self.rule_version, str)
            or not self.rule_version
            or type(self.review_revision) is not int
            or self.review_revision < 0
            or not isinstance(self.evidence_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.evidence_digest)
        ):
            raise FeatureInputError("FEATURE_FIX_INVALID")


@dataclass(frozen=True)
class CommitInput:
    sha: str
    parents: tuple[str, ...]
    event_time: datetime
    identity_key: str
    files: tuple[FileDelta, ...]
    parse_status: str = "parsed"
    fix: FixSignal | None = None

    def __post_init__(self):
        utc(self.event_time)
        if not isinstance(self.sha, str) or not SHA.fullmatch(self.sha):
            raise FeatureInputError("FEATURE_SHA_INVALID")
        if type(self.parents) is not tuple or type(self.files) is not tuple:
            raise FeatureInputError("FEATURE_FROZEN_INPUT_REQUIRED")
        if (
            any(not isinstance(p, str) or not SHA.fullmatch(p) for p in self.parents)
            or len(set(self.parents)) != len(self.parents)
            or not isinstance(self.identity_key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.identity_key)
            or any(not isinstance(f, FileDelta) for f in self.files)
            or len({f.path for f in self.files}) != len(self.files)
            or not isinstance(self.parse_status, str)
            or self.fix is not None
            and not isinstance(self.fix, FixSignal)
        ):
            raise FeatureInputError("FEATURE_COMMIT_INVALID")

    @property
    def order(self):
        return self.event_time, self.sha


def freeze_record(record, fix=None):
    try:
        return _freeze_record(record, fix)
    except (KeyError, TypeError, AttributeError):
        raise FeatureInputError("FEATURE_RECORD_INVALID") from None


def _freeze_record(record, fix):
    """Project only necessary parser fields; drop message, aliases, emails and diff."""
    commit, identity = record["commit"], record["identity"]
    if (
        commit["parser_version"] != PARSER_VERSION
        or identity["identity_version"] != IDENTITY_VERSION
        or any(f["parser_version"] != PARSER_VERSION for f in record["files"])
        or commit["parent_count"] != len(commit["parents"])
    ):
        raise FeatureInputError("FEATURE_VERSION_CONFLICT")
    return CommitInput(
        commit["sha"],
        tuple(commit["parents"]),
        commit["committer_time"],
        identity["identity_key"],
        tuple(
            FileDelta(**{key: f[key] for key in FileDelta.__dataclass_fields__})
            for f in record["files"]
        ),
        commit["parse_status"],
        fix,
    )


@dataclass(frozen=True)
class Snapshot:
    repository_id: str
    head_sha: str
    commits: tuple[CommitInput, ...]
    history_coverage: str = "full"
    parser_version: str = PARSER_VERSION
    identity_version: str = IDENTITY_VERSION

    def __post_init__(self):
        if type(self.commits) is not tuple or any(
            not isinstance(c, CommitInput) for c in self.commits
        ):
            raise FeatureInputError("FEATURE_FROZEN_INPUT_REQUIRED")
        if self.parser_version != PARSER_VERSION or self.identity_version != IDENTITY_VERSION:
            raise FeatureInputError("FEATURE_VERSION_CONFLICT")
        if (
            self.history_coverage not in ("full", "recent_window")
            or not isinstance(self.repository_id, str)
            or not self.repository_id
            or not isinstance(self.head_sha, str)
            or not SHA.fullmatch(self.head_sha)
        ):
            raise FeatureInputError("FEATURE_SNAPSHOT_INVALID")
        index = {c.sha: c for c in self.commits}
        if len(index) != len(self.commits):
            raise FeatureInputError("FEATURE_DUPLICATE_SHA")
        if self.head_sha not in index:
            raise FeatureInputError("FEATURE_HEAD_MISSING")
        # Iterative DFS: long histories must not hit Python's recursion limit.
        active, visited, stack = set(), set(), [(self.head_sha, False)]
        while stack:
            sha, exiting = stack.pop()
            if exiting:
                active.remove(sha)
                visited.add(sha)
                continue
            if sha in active:
                raise FeatureInputError("FEATURE_DAG_CYCLE")
            if sha in visited:
                continue
            if sha not in index:
                if self.history_coverage == "full":
                    raise FeatureInputError("FEATURE_HISTORY_INCOMPLETE")
                continue
            active.add(sha)
            stack.append((sha, True))
            stack.extend((parent, False) for parent in index[sha].parents)
        if visited != set(index):
            raise FeatureInputError("FEATURE_UNACCEPTED_COMMIT")


@dataclass(frozen=True)
class FeatureVector:
    sha: str
    event_time: datetime
    source_digest: str
    values: tuple[tuple[str, int | float | None], ...]
    reasons: tuple[tuple[str, str], ...]
    feature_version: str = FEATURE_VERSION

    def __post_init__(self):
        utc(self.event_time)
        if (
            not isinstance(self.sha, str)
            or not SHA.fullmatch(self.sha)
            or not isinstance(self.source_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.source_digest)
            or self.feature_version != FEATURE_VERSION
            or type(self.values) is not tuple
            or type(self.reasons) is not tuple
            or any(type(row) is not tuple or len(row) != 2 for row in self.values + self.reasons)
            or tuple(row[0] for row in self.values) != FEATURES
            or any(
                key not in FEATURES or not isinstance(reason, str) or not reason
                for key, reason in self.reasons
            )
            or len({key for key, _ in self.reasons}) != len(self.reasons)
        ):
            raise FeatureInputError("FEATURE_VECTOR_INVALID")
        for key, value in self.values:
            if value is None:
                if key not in dict(self.reasons):
                    raise FeatureInputError("FEATURE_VECTOR_INVALID")
            elif type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise FeatureInputError("FEATURE_VALUE_INVALID")
        if dict(self.values)["FIX"] not in (None, 0, 1):
            raise FeatureInputError("FEATURE_VALUE_INVALID")

    def as_dict(self):
        return {
            "sha": self.sha,
            "event_time": self.event_time.isoformat() + "Z",
            "feature_version": self.feature_version,
            "source_digest": self.source_digest,
            "values": dict(self.values),
            "reasons": dict(self.reasons),
            "missing": [key for key, value in self.values if value is None],
        }


def subsystem(path):
    return path.split("/", 1)[0] if "/" in path else "."


def ancestry(commit, index):
    seen, pending = set(), list(commit.parents)
    while pending:
        sha = pending.pop()
        if sha not in seen and sha in index:
            seen.add(sha)
            pending.extend(index[sha].parents)
    return sorted((index[sha] for sha in seen), key=lambda c: c.order)


def file_history(file, previous):
    if file.change_type in {"A", "C"}:
        return []
    path, changes = file.old_path, []
    for commit in reversed(previous):
        for changed in commit.files:
            if changed.new_path == path:
                changes.append(commit)
                if changed.change_type in {"A", "C"}:
                    return changes
                path = changed.old_path
                break
            if changed.change_type == "D" and changed.old_path == path:
                return changes
    return changes


def vector(commit, previous, snapshot, source, purpose):
    values, reasons = dict.fromkeys(FEATURES), {}

    def missing(keys, reason):
        for key in keys:
            values[key], reasons[key] = None, reason

    valid = (
        commit.parse_status == "parsed"
        and len(commit.parents) <= 1
        and all(f.supported for f in commit.files)
    )
    if not valid:
        missing(FEATURES, "current_content_unsupported")
    else:
        paths = [f.path for f in commit.files]
        churn = [f.insertions + f.deletions for f in commit.files]
        total = sum(churn)
        values.update(
            NS=len({subsystem(p) for p in paths}),
            ND=len({p.rsplit("/", 1)[0] if "/" in p else "." for p in paths}),
            NF=len(paths),
            LA=sum(f.insertions for f in commit.files),
            LD=sum(f.deletions for f in commit.files),
            LT=sum(f.old_loc for f in commit.files),
            Entropy=(
                -math.fsum((n / total) * math.log2(n / total) for n in churn if n)
                / math.log2(len(paths))
                if total and len(paths) > 1
                else 0.0
            ),
        )
        if snapshot.history_coverage != "full":
            missing(HISTORY, "history_incomplete")
        elif any(c.order >= commit.order for c in previous):
            missing(HISTORY, "ancestor_time_inversion")
        else:
            by_author = [c for c in previous if c.identity_key == commit.identity_key]
            values["EXP"] = len(by_author)
            values["REXP"] = math.fsum(
                1 / (1 + (commit.event_time - c.event_time).total_seconds() / (86400 * 365.25))
                for c in by_author
            )
            incomplete = any(
                c.parse_status != "parsed" or any(not f.supported for f in c.files)
                for c in previous
            )
            if incomplete:
                missing(("SEXP", "NDEV", "AGE", "NUC"), "ancestor_content_unsupported")
            else:
                systems = {subsystem(p) for p in paths}
                values["SEXP"] = sum(
                    bool(systems & {subsystem(f.path) for f in c.files}) for c in by_author
                )
                histories = [file_history(f, previous) for f in commit.files]
                values["NDEV"] = len({c.identity_key for h in histories for c in h})
                values["NUC"] = len({h[0].sha for h in histories if h})
                if not total:
                    missing(("AGE",), "zero_churn")
                elif any(not h for h, weight in zip(histories, churn, strict=True) if weight):
                    missing(("AGE",), "first_file_change")
                else:
                    values["AGE"] = (
                        math.fsum(
                            weight * (commit.event_time - h[0].event_time).total_seconds() / 86400
                            for h, weight in zip(histories, churn, strict=True)
                            if weight
                        )
                        / total
                    )
    if purpose == "candidate":
        values["FIX"], reasons["FIX"] = 0, "candidate_fix_default"
    elif commit.fix is None:
        missing(("FIX",), "fix_unavailable")
    elif commit.fix.available_at > commit.event_time:
        missing(("FIX",), "fix_not_yet_available")
    else:
        values["FIX"] = int(commit.fix.value)
        reasons.pop("FIX", None)
    return FeatureVector(
        commit.sha, commit.event_time, source, tuple(values.items()), tuple(sorted(reasons.items()))
    )


def compute(snapshot, purpose="historical"):
    if not isinstance(snapshot, Snapshot) or purpose not in ("historical", "candidate"):
        raise FeatureInputError("FEATURE_POLICY_UNSUPPORTED")
    ordered = sorted(snapshot.commits, key=lambda c: c.order)
    source = digest(
        {
            "repository_id": snapshot.repository_id,
            "head_sha": snapshot.head_sha,
            "coverage": snapshot.history_coverage,
            "parser_version": snapshot.parser_version,
            "identity_version": snapshot.identity_version,
            "feature_version": FEATURE_VERSION,
            "rule_hash": RULE_HASH,
            "purpose": purpose,
            "commits": [asdict(c) for c in ordered],
        }
    )
    index = {c.sha: c for c in ordered}
    return tuple(vector(c, ancestry(c, index), snapshot, source, purpose) for c in ordered)


def quality(vectors):
    """One immutable run only; train preprocessing and label distributions are separate."""
    vectors = tuple(vectors)
    if (
        any(not isinstance(v, FeatureVector) for v in vectors)
        or len({v.sha for v in vectors}) != len(vectors)
        or len({(v.source_digest, v.feature_version) for v in vectors}) > 1
    ):
        raise FeatureInputError("FEATURE_QUALITY_POPULATION_INVALID")
    report = {}
    for name in FEATURES:
        raw = sorted(dict(v.values)[name] for v in vectors if dict(v.values)[name] is not None)
        if any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in raw):
            raise FeatureInputError("FEATURE_VALUE_INVALID")

        def quantile(p):
            if not raw:
                return None
            index = (len(raw) - 1) * p
            low, high = math.floor(index), math.ceil(index)
            return raw[low] + (raw[high] - raw[low]) * (index - low)

        report[name] = {
            "valid": len(raw),
            "missing": len(vectors) - len(raw),
            "missing_reasons": dict(
                sorted(
                    Counter(
                        dict(v.reasons)[name] for v in vectors if dict(v.values)[name] is None
                    ).items()
                )
            ),
            "min": raw[0] if raw else None,
            "p25": quantile(0.25),
            "median": quantile(0.5),
            "p75": quantile(0.75),
            "p95": quantile(0.95),
            "max": raw[-1] if raw else None,
        }
    return {
        "count": len(vectors),
        "feature_version": FEATURE_VERSION,
        "source_digest": vectors[0].source_digest if vectors else None,
        "features": report,
    }
