"""Local immutable review declarations; never modify labels or promote readiness."""

import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from packages.mining.public_assets import (
    ROOT,
    AssetError,
    Budget,
    atomic_json,
    file_digest,
    load_asset,
    normalized_rows,
    read_json,
    safe_path,
    sealed,
    utc_time,
    verify_seal,
)
from packages.mining.source_evidence import VERSION as SOURCE_VERSION
from packages.mining.source_evidence import content_hash

VERSION = "source-review-declarations-v1"
STATUSES = {"pending", "unresolved", "conflict", "filter_supported"}
MAX_REVISIONS = 100


def hex_id(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise AssetError("REVIEW_ID_INVALID")
    return value


def context(report_path, asset_path, *, tick=None):
    budget = Budget(tick=tick)
    asset = load_asset(asset_path, tick=budget.check)
    report = read_json(report_path, budget.check)
    verify_seal(report)
    if (
        report.get("version") != SOURCE_VERSION
        or report.get("published_acquisition")
        != asset["manifest"]["identity"]["source_acquisition"]
    ):
        raise AssetError("REVIEW_SOURCE_CONFLICT")
    if (
        report.get("product_training_ready") is not False
        or report.get("gold_review_complete") is not False
        or report.get("historical_available_at") is not None
    ):
        raise AssetError("REVIEW_SCOPE_INVALID")
    packets = report.get("review_packet")
    if not isinstance(packets, list) or not 1 <= len(packets) <= 200:
        raise AssetError("REVIEW_PACKET_LIMIT")
    seen = set()
    for p in packets:
        if (
            not isinstance(p, dict)
            or not isinstance(p.get("project"), str)
            or not re.fullmatch("[0-9a-f]{40}", str(p.get("commit_id")))
        ):
            raise AssetError("REVIEW_PACKET_INVALID")
        member = p["project"], p["commit_id"]
        if (
            member in seen
            or p.get("review_state") != "pending"
            or p.get("historical_available_at") is not None
            or p.get("negative_coverage") != "unknown"
        ):
            raise AssetError("REVIEW_PACKET_INVALID")
        seen.add(member)
        hex_id(p.get("raw_links_digest"))
        if p["project"] not in report.get("projects", {}):
            raise AssetError("REVIEW_PACKET_INVALID")
        hex_id(report["projects"][p["project"]].get("member_digest"))
    wanted = {(p["project"], p["commit_id"]): p["published_label"] for p in packets}
    matched = {}
    for row in normalized_rows(safe_path(asset_path) / "source/apachejit_total.csv", budget.check):
        sample = row["cohort"], row["commit_id"]
        if sample in wanted:
            matched[sample] = row["published_label"]
    if matched != wanted:
        raise AssetError("REVIEW_ASSET_MEMBER_CONFLICT")
    binding = {
        "version": VERSION,
        "asset_id": asset["manifest"]["asset_id"],
        "report_hash": report["content_hash"],
        "asset_path": safe_path(asset_path).relative_to(ROOT).as_posix(),
        "report_path": safe_path(report_path).relative_to(ROOT).as_posix(),
    }
    return binding, report


def initialize_reviews(report_path, asset_path, store, *, tick=None):
    budget = Budget(tick=tick)
    binding, report = context(report_path, asset_path, tick=budget.check)
    store = safe_path(store)
    if store.is_relative_to(safe_path(asset_path).parent):
        raise AssetError("REVIEW_OUTPUT_COLLISION")
    context_id = content_hash(binding)
    directory = safe_path(Path(store) / context_id)
    if directory.is_relative_to(safe_path(asset_path)):
        raise AssetError("REVIEW_OUTPUT_COLLISION")
    initial = safe_path(directory / "initial.json")
    if initial.exists():
        return load_initial(directory, tick=budget.check)
    observed_at = datetime.now(UTC).isoformat()
    records = []
    for packet in report["review_packet"]:
        case_id = content_hash({"context": context_id, "packet": packet})
        records.append(
            sealed(
                {
                    "case_id": case_id,
                    "context_id": context_id,
                    "revision": 0,
                    "previous_hash": None,
                    "status": "pending",
                    "recorded_at": observed_at,
                    "operator": None,
                    "rationale": None,
                    "evidence": [],
                    "packet": packet,
                    "member_digest": report["projects"][packet["project"]]["member_digest"],
                    "gold_certified": False,
                    "historical_available_at": None,
                    "label_change_performed": False,
                }
            )
        )
    value = sealed(
        {
            "context_id": context_id,
            "binding": binding,
            "created_at": observed_at,
            "records": records,
        }
    )
    try:
        atomic_json(initial, value, check=budget.check)
    except FileExistsError:
        return load_initial(directory, tick=budget.check)
    return load_initial(directory, tick=budget.check)


def load_initial(directory, *, tick=None):
    directory = safe_path(directory)
    budget = Budget(tick=tick)
    value = read_json(directory / "initial.json", budget.check)
    verify_seal(value)
    if set(value) != {"context_id", "binding", "created_at", "records", "content_hash"}:
        raise AssetError("REVIEW_INITIAL_INVALID")
    binding = value["binding"]
    if (
        not isinstance(binding, dict)
        or set(binding) != {"version", "asset_id", "report_hash", "asset_path", "report_path"}
        or binding["version"] != VERSION
    ):
        raise AssetError("REVIEW_BINDING_INVALID")
    expected, report = context(
        ROOT / binding["report_path"], ROOT / binding["asset_path"], tick=budget.check
    )
    if (
        expected != binding
        or value["context_id"] != content_hash(binding)
        or directory.name != value["context_id"]
    ):
        raise AssetError("REVIEW_BINDING_INVALID")
    utc_time(value["created_at"])
    expected_records = []
    for packet in report["review_packet"]:
        case_id = content_hash({"context": value["context_id"], "packet": packet})
        expected_records.append(
            sealed(
                {
                    "case_id": case_id,
                    "context_id": value["context_id"],
                    "revision": 0,
                    "previous_hash": None,
                    "status": "pending",
                    "recorded_at": value["created_at"],
                    "operator": None,
                    "rationale": None,
                    "evidence": [],
                    "packet": packet,
                    "member_digest": report["projects"][packet["project"]]["member_digest"],
                    "gold_certified": False,
                    "historical_available_at": None,
                    "label_change_performed": False,
                }
            )
        )
    if value["records"] != expected_records:
        raise AssetError("REVIEW_MEMBER_CONFLICT")
    return value


def evidence_record(path, binding, case_id, check):
    value = read_json(path, check)
    verify_seal(value)
    if value.get("version") not in {"native-link-probe-v1", "source-filter-notes-v1"} or (
        value.get("asset_id"),
        value.get("report_hash"),
        value.get("case_id"),
    ) != (binding["asset_id"], binding["report_hash"], case_id):
        raise AssetError("REVIEW_EVIDENCE_CONFLICT")
    utc_time(value.get("observed_at"))
    return {
        "path": safe_path(path).relative_to(ROOT).as_posix(),
        **file_digest(path, check, maximum=1024**2),
        "content_hash": value["content_hash"],
    }


def case_history(directory, case_id, *, tick=None):
    hex_id(case_id)
    budget = Budget(tick=tick)
    initial = load_initial(directory, tick=budget.check)
    first = next((r for r in initial["records"] if r["case_id"] == case_id), None)
    if first is None:
        raise AssetError("REVIEW_CASE_MISSING")
    records = [first]
    folder = safe_path(Path(directory) / "cases" / case_id)
    if folder.exists():
        paths = []
        pending = 0
        for p in folder.iterdir():
            budget.check()
            # atomic_json publishes by hard link only after this private file is complete.
            # A concurrent reader must observe committed revisions, not writer staging.
            staging = re.fullmatch(r"\.([0-9]{6})\.json\.[0-9a-f]{32}\.part", p.name)
            if staging:
                if not 1 <= int(staging[1]) <= MAX_REVISIONS:
                    raise AssetError("REVIEW_CHAIN_INVALID")
                pending += 1
                if pending > MAX_REVISIONS:
                    raise AssetError("REVIEW_REVISION_LIMIT")
                try:
                    safe_path(p)
                    mode = p.lstat().st_mode
                except FileNotFoundError:
                    continue  # The publishing writer has already removed its own staging.
                if not stat.S_ISREG(mode):
                    raise AssetError("REVIEW_CHAIN_INVALID")
                continue
            safe_path(p)
            if not re.fullmatch("[0-9]{6}\\.json", p.name) or not p.is_file():
                raise AssetError("REVIEW_CHAIN_INVALID")
            paths.append(p)
            if len(paths) > MAX_REVISIONS:
                raise AssetError("REVIEW_REVISION_LIMIT")
        for revision, path in enumerate(sorted(paths), 1):
            if path.name != f"{revision:06d}.json":
                raise AssetError("REVIEW_CHAIN_INVALID")
            row = read_json(path, budget.check)
            verify_seal(row)
            if set(row) != {
                "case_id",
                "context_id",
                "revision",
                "previous_hash",
                "status",
                "recorded_at",
                "operator",
                "rationale",
                "evidence",
                "gold_certified",
                "historical_available_at",
                "label_change_performed",
                "content_hash",
            } or (
                row.get("case_id"),
                row.get("context_id"),
                row.get("revision"),
                row.get("previous_hash"),
            ) != (case_id, initial["context_id"], revision, records[-1]["content_hash"]):
                raise AssetError("REVIEW_CHAIN_INVALID")
            validate_declaration(row)
            if datetime.fromisoformat(utc_time(row["recorded_at"])) < datetime.fromisoformat(
                utc_time(records[-1]["recorded_at"])
            ):
                raise AssetError("REVIEW_TIME_CONFLICT")
            for item in row["evidence"]:
                if (
                    not isinstance(item, dict)
                    or evidence_record(
                        ROOT / item["path"], initial["binding"], case_id, budget.check
                    )
                    != item
                ):
                    raise AssetError("REVIEW_EVIDENCE_CHANGED")
            records.append(row)
    return {"initial": initial, "records": records}


def validate_declaration(row):
    if (
        not isinstance(row["status"], str)
        or row["status"] not in STATUSES
        or not isinstance(row["operator"], str)
        or not re.fullmatch("[A-Za-z0-9_.-]{1,64}", row["operator"])
    ):
        raise AssetError("REVIEW_DECLARATION_INVALID")
    if (
        not isinstance(row["rationale"], str)
        or not 1 <= len(row["rationale"]) <= 1024
        or any(ord(c) < 32 for c in row["rationale"])
    ):
        raise AssetError("REVIEW_DECLARATION_INVALID")
    if (
        not isinstance(row["evidence"], list)
        or len(row["evidence"]) > 8
        or row["status"] == "filter_supported"
        and not row["evidence"]
    ):
        raise AssetError("REVIEW_DECLARATION_INVALID")
    if (
        row["gold_certified"] is not False
        or row["historical_available_at"] is not None
        or row["label_change_performed"] is not False
    ):
        raise AssetError("REVIEW_SCOPE_INVALID")
    utc_time(row["recorded_at"])


def append_review(
    directory,
    case_id,
    *,
    expected_revision,
    expected_hash,
    status,
    operator,
    rationale,
    evidence_paths=(),
    tick=None,
):
    if (
        type(expected_revision) is not int
        or not 0 <= expected_revision < MAX_REVISIONS
        or len(evidence_paths) > 8
    ):
        raise AssetError("REVIEW_REVISION_INVALID")
    hex_id(expected_hash)
    budget = Budget(tick=tick)
    history = case_history(directory, case_id, tick=budget.check)
    previous, initial = history["records"][-1], history["initial"]
    if (expected_revision, expected_hash) != (previous["revision"], previous["content_hash"]):
        raise AssetError("REVIEW_CONFLICT")
    evidence = [
        evidence_record(p, initial["binding"], case_id, budget.check) for p in evidence_paths
    ]
    if len({e["path"] for e in evidence}) != len(evidence):
        raise AssetError("REVIEW_EVIDENCE_DUPLICATE")
    row = sealed(
        {
            "case_id": case_id,
            "context_id": initial["context_id"],
            "revision": expected_revision + 1,
            "previous_hash": previous["content_hash"],
            "status": status,
            "recorded_at": datetime.now(UTC).isoformat(),
            "operator": operator,
            "rationale": rationale,
            "evidence": evidence,
            "gold_certified": False,
            "historical_available_at": None,
            "label_change_performed": False,
        }
    )
    validate_declaration(row)
    output = safe_path(Path(directory) / "cases" / case_id / f"{row['revision']:06d}.json")
    try:
        atomic_json(output, row, check=budget.check)
    except (FileExistsError, AssetError) as exc:
        if output.exists():
            raise AssetError("REVIEW_CONFLICT") from exc
        raise
    return row
