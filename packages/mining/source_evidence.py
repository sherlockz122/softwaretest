"""Read-only candidate provenance audit; this never certifies observed negatives."""

import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from packages.mining.public_dataset import (
    DATE_FIELDS,
    PublicDataError,
    boolean,
    epoch,
    fingerprint,
    key,
    parse_date,
)
from packages.mining.source_catalog import PROJECTS

VERSION = "apachejit-raw-provenance-v1"
LINK_FIELDS = ("fix_hash", "fix_date", "bug_hash", "bug_date", "project")
MAX_BYTES = 100 * 1024**2
MAX_ROWS = 500000
MAX_SECONDS = 120


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def content_hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class SourceAudit:
    def __init__(self, value):
        self._bytes = canonical(value)

    def as_dict(self):
        return json.loads(self._bytes)


def acquisition_time(value):
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise PublicDataError("SOURCE_ACQUISITION_INVALID") from exc
    if result.tzinfo is None or not datetime(2022, 1, 1, tzinfo=UTC) <= result <= datetime.now(UTC):
        raise PublicDataError("SOURCE_ACQUISITION_INVALID")
    return result.astimezone(UTC).isoformat()


def published_epoch(value):
    parsed = parse_date(value)
    if parsed is None:
        return None
    # The fixed derivative encodes committer UTC as ISO text; raw links use epoch integers.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def rows(path, fields, check, consume):
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != fields:
                raise PublicDataError("SOURCE_SCHEMA_INVALID")
            for row in reader:
                check()
                consume()
                if set(row) != set(fields) or any(value is None for value in row.values()):
                    raise PublicDataError("SOURCE_ROW_INVALID")
                yield row
    except (UnicodeError, csv.Error) as exc:
        raise PublicDataError("SOURCE_ENCODING_INVALID") from exc


def audit_sources(
    date_path,
    link_paths,
    *,
    acquired_at,
    max_bytes=MAX_BYTES,
    max_rows=MAX_ROWS,
    seconds=MAX_SECONDS,
    tick=None,
):
    """The published date side is separately joined/validated by the CLI's public audit."""
    acquired = acquisition_time(acquired_at)
    if (
        type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_BYTES
        or type(max_rows) is not int
        or not 1 <= max_rows <= MAX_ROWS
        or type(seconds) not in (int, float)
        or not 0 < seconds <= MAX_SECONDS
        or not link_paths
        or any(code not in PROJECTS for code in link_paths)
    ):
        raise PublicDataError("SOURCE_LIMIT_INVALID")
    paths = {"published_dates": Path(date_path)}
    paths.update({code: Path(path) for code, path in sorted(link_paths.items())})
    if len({path.resolve() for path in paths.values()}) != len(paths):
        raise PublicDataError("SOURCE_PATH_COLLISION")
    deadline = time.monotonic() + seconds
    consumed = 0

    def check():
        if tick:
            tick()
        if time.monotonic() > deadline:
            raise PublicDataError("SOURCE_DEADLINE")

    def consume():
        nonlocal consumed
        consumed += 1
        if consumed > max_rows:
            raise PublicDataError("SOURCE_ROW_LIMIT")

    def hashes():
        check()
        if sum(path.stat().st_size for path in paths.values()) > max_bytes:
            raise PublicDataError("SOURCE_BYTE_LIMIT")
        return {
            name: fingerprint(path, max_bytes=max_bytes, check=check)
            for name, path in paths.items()
        }

    before = hashes()
    links = defaultdict(set)
    raw = {}
    for code in sorted(link_paths):
        counts = Counter()
        pairs = defaultdict(set)
        for row in rows(paths[code], LINK_FIELDS, check, consume):
            if row["project"] != code or any(
                not re.fullmatch("[0-9a-f]{40}", row[name]) for name in ("fix_hash", "bug_hash")
            ):
                raise PublicDataError("SOURCE_LINK_ID_INVALID")
            fix_time, bug_time = epoch(row["fix_date"]), epoch(row["bug_date"])
            item = (row["fix_hash"], fix_time, bug_time)
            linked = links[(PROJECTS[code], row["bug_hash"])]
            counts["rows"] += 1
            counts["duplicate_rows"] += item in linked
            counts["non_forward_clock_rows"] += fix_time <= bug_time
            counts["self_link_rows"] += row["fix_hash"] == row["bug_hash"]
            linked.add(item)
            pairs[(row["fix_hash"], row["bug_hash"])].add((fix_time, bug_time))
        counts["unique_pairs"] = len(pairs)
        counts["pairs_with_conflicting_clocks"] = sum(len(v) > 1 for v in pairs.values())
        counts["unique_bug_candidates"] = sum(p == PROJECTS[code] for p, _ in links)
        raw[PROJECTS[code]] = dict(sorted(counts.items()))

    projects = defaultdict(Counter)
    members = defaultdict(list)
    strata = defaultdict(list)
    seen = set()
    for row in rows(paths["published_dates"], DATE_FIELDS, check, consume):
        project, sha = key(row)
        if (project, sha) in seen:
            raise PublicDataError("SOURCE_DUPLICATE_KEY")
        seen.add((project, sha))
        buggy, fixing = boolean(row["buggy"]), boolean(row["fix"])
        if not re.fullmatch("[0-9]{4}", row["year"]):
            raise PublicDataError("SOURCE_YEAR_INVALID")
        event = epoch(row["date"])
        bug_time = published_epoch(row["bug_date"])
        published_fix = published_epoch(row["fix_date"])
        linked = sorted(links.get((project, sha), ()), key=lambda x: (x[1], x[0], x[2]))
        status = (
            "source_missing"
            if project not in raw
            else ("raw_candidate" if linked else "no_raw_candidate")
        )
        label = "buggy" if buggy else "clean"
        counts = projects[project]
        counts["rows"] += 1
        counts[label] += 1
        counts[f"{label}_{status}"] += 1
        if buggy:
            counts["bug_clock_missing_or_differs"] += bug_time is None or bug_time != event
            if linked:
                counts["raw_bug_clock_mismatch"] += not any(x[2] == event for x in linked)
                matched = any(x[1] == published_fix for x in linked)
                counts["selected_fix_matches_raw"] += matched
                counts["selected_fix_missing_or_unmatched"] += not matched
                counts["selected_fix_not_earliest_raw"] += matched and published_fix != linked[0][1]
        elif bug_time is not None or published_fix is not None:
            counts["clean_with_positive_clock_metadata"] += 1
        packet = {
            "project": project,
            "commit_id": sha,
            "published_label": label,
            "published_fix_flag": fixing,
            "event_epoch": event,
            "selected_fix_epoch": published_fix,
            "raw_link_status": status,
            "raw_link_count": len(linked),
            "raw_links_digest": content_hash(linked),
            "earliest_raw_fix_epoch": linked[0][1] if linked else None,
            "display_links": [
                {"fix_hash": fix, "fix_epoch": ft, "bug_epoch": bt} for fix, ft, bt in linked[:8]
            ],
            "display_links_truncated": len(linked) > 8,
            "historical_available_at": None,
            "review_state": "pending",
            "negative_coverage": "unknown",
        }
        bucket = (project, label, status)
        rank = content_hash({"version": VERSION, "key": (project, sha), "label": label})
        strata[bucket].append((rank, packet))
        # Retain only the quota; digest the whole cohort separately.
        strata[bucket] = sorted(strata[bucket], key=lambda x: x[0])[:2]
        members[project].append((sha, label, event, published_fix, packet["raw_links_digest"]))

    if not seen:
        raise PublicDataError("SOURCE_EMPTY_PUBLISHED")
    after = hashes()
    if before != after:
        raise PublicDataError("SOURCE_INPUT_CHANGED")
    check()
    rule_paths = (
        Path(__file__),
        Path(__file__).with_name("public_dataset.py"),
        Path(__file__).with_name("source_catalog.py"),
    )
    result = {
        "version": VERSION,
        "rule_hash": content_hash({p.name: p.read_text(encoding="utf-8") for p in rule_paths}),
        "source_sha256": before,
        "acquired_at": acquired,
        "acquisition_semantics": "verified_bundle_capture_not_historical_knowledge",
        "historical_available_at": None,
        "clock_semantics": "upstream_committer_epoch_UTC_not_knowledge",
        "rows": len(seen),
        "input_rows": consumed,
        "raw_projects": raw,
        "projects": {
            p: {
                "counts": dict(sorted(v.items())),
                "member_digest": content_hash(sorted(members[p])),
            }
            for p, v in sorted(projects.items())
        },
        "review_sampling": "sha256_rank_quota_2_per_project_label_link_status_not_noise_estimate",
        "review_packet": [packet for _, values in sorted(strata.items()) for _, packet in values],
        "candidate_provenance_checks_complete": True,
        "filter_reproduction_complete": False,
        "negative_coverage_complete": False,
        "native_ancestry_certified": False,
        "gold_review_complete": False,
        "noise_rate": None,
        "compatible_fourteen_features": False,
        "historical_replay_ready": False,
        "product_training_ready": False,
        "training_deficits": [
            "upstream_issue_and_AST_filter_reproduction_missing",
            "native_link_ancestry_and_clock_certification_missing",
            "complete_negative_observation_and_censoring_evidence_missing",
            "historical_evidence_available_at_missing",
            "silver_review_and_noise_assessment_pending",
            "compatible_fourteen_features_missing",
        ],
    }
    check()
    result["content_hash"] = content_hash(result)
    return SourceAudit(result)
