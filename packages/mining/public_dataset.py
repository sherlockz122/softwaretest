"""Read-only published research-data audit, separate from production time labels."""

import csv
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

VERSION = "apachejit-published-13-v1"
TOTAL_FIELDS = (
    "commit_id",
    "project",
    "buggy",
    "fix",
    "year",
    "author_date",
    "la",
    "ld",
    "nf",
    "nd",
    "ns",
    "ent",
    "ndev",
    "age",
    "nuc",
    "aexp",
    "arexp",
    "asexp",
)
DATE_FIELDS = ("commit_id", "project", "bug_date", "fix_date", "buggy", "fix", "date", "year")
NUMERIC_FIELDS = TOTAL_FIELDS[6:]


class PublicDataError(ValueError):
    """Stable safe errors; never include upstream row contents."""


def fingerprint(path, *, max_bytes=None, check=None):
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if max_bytes is not None and count > max_bytes:
                raise PublicDataError("PUBLIC_FILE_LIMIT")
            if check:
                check()
            digest.update(chunk)
    return digest.hexdigest()


def boolean(value):
    if value not in ("True", "False"):
        raise PublicDataError("PUBLIC_BOOLEAN_INVALID")
    return value == "True"


def epoch(value):
    if not re.fullmatch(r"[0-9]{1,11}", value):
        raise PublicDataError("PUBLIC_EVENT_INVALID")
    result = int(value)
    if not 0 <= result <= 4102444800:
        raise PublicDataError("PUBLIC_EVENT_INVALID")
    return result


def key(row):
    if not re.fullmatch(r"[0-9a-f]{40}", row["commit_id"]) or not re.fullmatch(
        r"apache/[a-z][a-z0-9-]{0,63}", row["project"]
    ):
        raise PublicDataError("PUBLIC_KEY_INVALID")
    return row["project"], row["commit_id"]


def parse_date(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PublicDataError("PUBLIC_FIX_DATE_INVALID") from exc
    if not 1970 <= result.year <= 2100:
        raise PublicDataError("PUBLIC_FIX_DATE_INVALID")
    return result


def partition(samples):
    """Keep each author-time group whole; never search for favorable class balance."""
    samples = sorted(samples)  # (epoch, SHA, buggy); scoped to one project by caller.
    groups = []
    for sample in samples:
        if not groups or groups[-1][0][0] != sample[0]:
            groups.append([])
        groups[-1].append(sample)
    result = {"time_groups": len(groups), "structurally_ready": False, "splits": {}}
    if len(groups) < 3:
        result["deficits"] = ["insufficient_time_groups"]
        return result
    cumulative, count = [], 0
    for group in groups:
        count += len(group)
        cumulative.append(count)
    first = min(next(i + 1 for i, n in enumerate(cumulative) if n >= count * 0.7), len(groups) - 2)
    second = min(
        max(next(i + 1 for i, n in enumerate(cumulative) if n >= count * 0.85), first + 1),
        len(groups) - 1,
    )
    deficits = []
    for name, selected in (
        ("train", groups[:first]),
        ("validation", groups[first:second]),
        ("test", groups[second:]),
    ):
        rows = [sample for group in selected for sample in group]
        classes = Counter("buggy" if sample[2] else "clean" for sample in rows)
        if set(classes) != {"buggy", "clean"}:
            deficits.append(name + "_missing_class")
        result["splits"][name] = {
            "rows": len(rows),
            "classes": dict(sorted(classes.items())),
            "first_event": rows[0][0],
            "last_event": rows[-1][0],
            "membership_sha256": hashlib.sha256(
                json.dumps(rows, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    result["deficits"] = deficits
    result["structurally_ready"] = not deficits
    return result


def audit_paths(total_path, date_path, *, max_rows=200000, max_bytes=100 * 1024**2, seconds=60):
    """Aggregate actual source disagreements without changing or fitting any values."""
    if (
        type(max_rows) is not int
        or max_rows < 1
        or type(max_bytes) is not int
        or max_bytes < 1
        or type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise PublicDataError("PUBLIC_LIMIT_INVALID")
    started = time.perf_counter()
    paths = [Path(total_path), Path(date_path)]
    for path in paths:
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise PublicDataError("PUBLIC_FILE_LIMIT")

    def budget():
        if time.perf_counter() - started > seconds:
            raise PublicDataError("PUBLIC_TIMEOUT")

    hashes = [fingerprint(path, max_bytes=max_bytes, check=budget) for path in paths]

    def rows(path, schema):
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != schema:
                raise PublicDataError("PUBLIC_SCHEMA_INVALID")
            for count, row in enumerate(reader, 1):
                budget()
                if count > max_rows:
                    raise PublicDataError("PUBLIC_ROW_LIMIT")
                if None in row or any(value is None for value in row.values()):
                    raise PublicDataError("PUBLIC_ROW_INVALID")
                if not re.fullmatch(r"(?:19|20)[0-9]{2}", row["year"]):
                    raise PublicDataError("PUBLIC_YEAR_INVALID")
                yield row

    dates = {}
    for row in rows(paths[1], DATE_FIELDS):
        row_key = key(row)
        if row_key in dates:
            raise PublicDataError("PUBLIC_DUPLICATE_KEY")
        dates[row_key] = (
            boolean(row["buggy"]),
            boolean(row["fix"]),
            epoch(row["date"]),
            parse_date(row["fix_date"]),
            int(row["year"]),
            parse_date(row["bug_date"]),
        )
    projects, seen = defaultdict(list), set()
    quality = Counter()
    ranges = {field: {"min": None, "max": None, "fractional": 0} for field in NUMERIC_FIELDS}
    for row in rows(paths[0], TOTAL_FIELDS):
        row_key = key(row)
        if row_key in seen:
            raise PublicDataError("PUBLIC_DUPLICATE_KEY")
        seen.add(row_key)
        buggy, fix, event = boolean(row["buggy"]), boolean(row["fix"]), epoch(row["author_date"])
        if row_key not in dates:
            raise PublicDataError("PUBLIC_JOIN_MISSING")
        other_buggy, other_fix, other_event, fixed, other_year, _ = dates[row_key]
        if (buggy, fix, int(row["year"])) != (other_buggy, other_fix, other_year):
            raise PublicDataError("PUBLIC_JOIN_CONFLICT")
        quality["event_clock_disagreements"] += event != other_event
        quality["year_vs_author_utc_disagreements"] += (
            int(row["year"]) != datetime.fromtimestamp(event, UTC).year
        )
        quality["positive_missing_fix_date"] += buggy and fixed is None
        quality["negative_with_fix_date"] += not buggy and fixed is not None
        if fixed is not None:
            quality["fix_dates_without_timezone"] += fixed.tzinfo is None
            # Naive dates are compared only as diagnostics under an explicit UTC assumption.
            diagnostic_epoch = (
                fixed.replace(tzinfo=UTC).timestamp() if fixed.tzinfo is None else fixed.timestamp()
            )
            quality["fix_before_author_assuming_utc"] += diagnostic_epoch < event
            quality["fix_before_derivative_event_assuming_utc"] += diagnostic_epoch < other_event
        for field in NUMERIC_FIELDS:
            try:
                value = float(row[field])
            except ValueError as exc:
                raise PublicDataError("PUBLIC_NUMERIC_INVALID") from exc
            if not math.isfinite(value) or value < 0:
                raise PublicDataError("PUBLIC_NUMERIC_INVALID")
            metrics = ranges[field]
            metrics["min"] = value if metrics["min"] is None else min(metrics["min"], value)
            metrics["max"] = value if metrics["max"] is None else max(metrics["max"], value)
            metrics["fractional"] += not value.is_integer()
        projects[row_key[0]].append((event, row_key[1], buggy))
    if seen != dates.keys():
        raise PublicDataError("PUBLIC_JOIN_EXTRA")
    budget()
    results = {}
    for project, samples in sorted(projects.items()):
        budget()
        results[project] = partition(samples)
    if [fingerprint(path, max_bytes=max_bytes, check=budget) for path in paths] != hashes:
        raise PublicDataError("PUBLIC_SOURCE_CHANGED")
    budget()
    labels = Counter(
        "buggy" if sample[2] else "clean" for samples in projects.values() for sample in samples
    )
    report = {
        "audit_version": VERSION,
        "audit_rule_sha256": hashlib.sha256(
            Path(__file__).read_text(encoding="utf-8").encode()
        ).hexdigest(),
        "purpose": "published_research_feasibility_only",
        "source_sha256": dict(zip(("apachejit_total", "neurojit_dates"), hashes, strict=True)),
        "rows": len(seen),
        "project_groups": len(projects),
        "labels": dict(sorted(labels.items())),
        "event_clock": "ApacheJIT author_date, epoch UTC; not project committer_time",
        "features": [*NUMERIC_FIELDS, "fix"],
        "feature_ranges": ranges,
        "date_quality": dict(sorted(quality.items())),
        "projects": results,
        "published_benchmark_structurally_ready": bool(results)
        and all(r["structurally_ready"] for r in results.values()),
        "structurally_eligible_projects": [
            p for p, r in results.items() if r["structurally_ready"]
        ],
        "historical_replay_ready": False,
        "product_training_ready": False,
        "blockers": [
            "missing_LT_and_incompatible_feature_semantics",
            "FIX_at_prediction_time_not_verified",
            "negative_observation_coverage_not_verified",
            "external_labels_not_product_confirmed_evidence",
            "historical_event_and_fix_time_provenance_not_verified",
        ],
        "preprocessing_fitted": False,
        "models_trained": 0,
        "split_policy": {
            "train": 0.7,
            "validation": 0.15,
            "test": 0.15,
            "unit": "per_project_author_time_group",
            "label_available_purge": "not_applied_no_verified_availability",
        },
    }
    report["content_hash"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return report
