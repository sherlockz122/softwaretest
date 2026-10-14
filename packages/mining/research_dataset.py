"""Versioned post-publication research: never certify historical or release labels."""

import csv
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from itertools import zip_longest
from pathlib import Path

from packages.mining import public_assets as assets
from packages.mining.public_dataset import NUMERIC_FIELDS, TOTAL_FIELDS, boolean, epoch, key
from packages.mining.source_evidence import canonical, content_hash

VERSION = "published-binary-12-v1"
PURPOSE = "published_binary_12_v1"
PARTITIONS = ("train", "validation", "test")
CSV_FIELDS = ("canonical_repository", "commit_id", "event_epoch", "label", *NUMERIC_FIELDS)
STORE = assets.ROOT / "runtime/public-assets"
OUTPUT = assets.ROOT / "runtime/datasets"
RULE_HASH = content_hash(
    {
        name: Path(__file__).with_name(name).read_text(encoding="utf-8").replace("\r\n", "\n")
        for name in (
            "research_dataset.py",
            "dataset_service.py",
            "public_assets.py",
            "public_dataset.py",
        )
    }
    | {
        "canonical_json.py": Path(__file__)
        .parents[1]
        .joinpath("persistence/canonical_json.py")
        .read_text(encoding="utf-8")
        .replace("\r\n", "\n")
    }
)


class ResearchError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def asset_path(asset_id, store=None):
    if not isinstance(asset_id, str) or not assets.re.fullmatch("[0-9a-f]{64}", asset_id):
        raise ResearchError("DATA_SOURCE_INVALID")
    return assets.safe_path((store or STORE) / asset_id)


def preview(asset_id, store=None):
    directory = asset_path(asset_id, store)
    budget = assets.Budget(seconds=10)
    manifest = assets.read_json(directory / "manifest.json", budget.check, maximum=128 * 1024)
    assets.verify_seal(manifest)
    identity = manifest.get("identity", {})
    if manifest.get("asset_id") != asset_id or content_hash(identity) != asset_id:
        raise ResearchError("DATA_SOURCE_INVALID")
    quality = assets.read_json(directory / "quality.json", budget.check)
    assets.verify_seal(quality)
    return {
        "asset_id": asset_id,
        "source_hash": manifest["content_hash"],
        "rows": quality.get("rows"),
        "labels": quality.get("labels"),
        "first_stored_at": manifest.get("first_stored_at"),
        "acquired_at": identity.get("source_acquisition", {}).get("acquired_at"),
        "integrity": "pending_worker_verification",
        "historical_replay": False,
        "candidate_release": False,
    }


def previews(store=None):
    directory = assets.safe_path(store or STORE)
    if not directory.exists():
        return []
    items = []
    for number, path in enumerate(directory.iterdir()):
        if number >= 100:
            raise ResearchError("DATA_CATALOG_LIMIT")
        if path.is_dir() and assets.re.fullmatch("[0-9a-f]{64}", path.name):
            items.append(preview(path.name, directory))
    return sorted(items, key=lambda x: x["asset_id"])


def policy_valid(policy):
    if set(policy) != {
        "purpose",
        "split_strategy",
        "train_ratio",
        "validation_ratio",
        "test_ratio",
    }:
        raise ResearchError("DATA_POLICY_INVALID")
    if policy["purpose"] != PURPOSE:
        raise ResearchError("DATA_PURPOSE_UNSUPPORTED")
    if policy["split_strategy"] not in {"time_ordered", "repository_holdout"}:
        raise ResearchError("DATA_POLICY_INVALID")
    ratios = [policy[k + "_ratio"] for k in PARTITIONS]
    if any(type(r) not in (int, float) or not math.isfinite(r) or not 0 < r < 1 for r in ratios):
        raise ResearchError("DATA_POLICY_INVALID")
    if not math.isclose(sum(ratios), 1, abs_tol=1e-12):
        raise ResearchError("DATA_POLICY_INVALID")
    if any(r != round(r, 6) for r in ratios):
        raise ResearchError("DATA_POLICY_INVALID")


def snapshot_bytes(directory, name, records, check):
    path = assets.safe_path(directory / name)
    with path.open("rb") as stream:
        data = stream.read(assets.MAX_BYTES + 1)
    record = next((r for r in records if r["path"] == name), None)
    if (
        not record
        or len(data) != record["bytes"]
        or hashlib.sha256(data).hexdigest() != record["sha256"]
    ):
        raise ResearchError("DATA_SOURCE_DRIFT")
    check()
    return data


def prepare(directory, source_hash, policy, *, tick=None):
    policy_valid(policy)
    loaded = assets.load_asset(directory, tick=tick)
    manifest, quality = loaded["manifest"], loaded["quality"]
    if manifest["content_hash"] != source_hash:
        raise ResearchError("DATA_SOURCE_DRIFT")
    acquisition = manifest["identity"]["source_acquisition"]
    if (
        acquisition.get("apachejit_license") != "CC-BY-4.0"
        or acquisition.get("upstream_code_executed") is not False
    ):
        raise ResearchError("DATA_LICENSE_UNVERIFIED")
    budget = assets.Budget(tick=tick)
    inventory = manifest["identity"]["files"]
    raw = snapshot_bytes(directory, "source/apachejit_total.csv", inventory, budget.check)
    normalized = snapshot_bytes(directory, "normalized.jsonl", inventory, budget.check)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    if tuple(reader.fieldnames or ()) != TOTAL_FIELDS:
        raise ResearchError("DATA_SOURCE_INVALID")
    rows = {}
    duplicate_rows = 0
    number = 0
    for number, pair in enumerate(zip_longest(reader, io.BytesIO(normalized)), 1):
        budget.check()
        original, line = pair
        if original is None or line is None or len(line) > 65536 or number > assets.MAX_ROWS:
            raise ResearchError("DATA_SOURCE_INVALID")
        if set(original) != set(TOTAL_FIELDS) or any(
            type(value) is not str or len(value) > 4096 for value in original.values()
        ):
            raise ResearchError("DATA_SOURCE_INVALID")
        try:
            record = json.loads(line)
            cohort, sha = key(original)
            values = {k: float(original[k]) for k in NUMERIC_FIELDS}
            canonical_repository = (
                "apache/hadoop"
                if cohort in {"apache/hadoop", "apache/hadoop-hdfs", "apache/hadoop-mapreduce"}
                else cohort
            )
            expected = {
                "row_number": number,
                "cohort": cohort,
                "canonical_repository": canonical_repository,
                "commit_id": sha,
                "sample_granularity": "commit",
                "original_fields": original,
                "event_epoch": epoch(original["author_date"]),
                "event_clock": "published_author_epoch_UTC",
                "published_label": "buggy" if boolean(original["buggy"]) else "clean",
                "label_confidence": "published_SZZ_weak_supervision",
                "label_available_at": None,
                "numeric12": values,
                "published_fix_metadata": boolean(original["fix"]),
                "defect_types": None,
                "type_status": "unknown",
                "location": None,
                "languages": [],
                "language_status": "per_row_unverified",
                "semantic_available": False,
            }
            if canonical(record) != canonical(expected) or any(
                not math.isfinite(x) or x < 0 for x in values.values()
            ):
                raise ResearchError("DATA_NORMALIZATION_DRIFT")
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            if isinstance(exc, ResearchError):
                raise
            raise ResearchError("DATA_SOURCE_INVALID") from None
        identity = (canonical_repository, sha)
        value = {
            "canonical_repository": canonical_repository,
            "commit_id": sha,
            "event_epoch": expected["event_epoch"],
            "label": int(expected["published_label"] == "buggy"),
            "numeric12": values,
            "source_rows": [number],
            "cohorts": [cohort],
        }
        if identity in rows:
            previous = rows[identity]
            if any(previous[k] != value[k] for k in ("event_epoch", "label", "numeric12")):
                raise ResearchError("DATA_DUPLICATE_CONFLICT")
            previous["source_rows"].append(number)
            previous["cohorts"] = sorted(set(previous["cohorts"]) | {cohort})
            duplicate_rows += 1
        else:
            rows[identity] = value
    members = sorted(
        rows.values(), key=lambda r: (r["event_epoch"], r["canonical_repository"], r["commit_id"])
    )
    if not members or number != quality["rows"]:
        raise ResearchError("DATA_SOURCE_INVALID")
    group_key = (
        (lambda r: r["event_epoch"])
        if policy["split_strategy"] == "time_ordered"
        else (lambda r: r["canonical_repository"])
    )
    groups = sorted({group_key(r) for r in members})
    if policy["split_strategy"] == "repository_holdout":
        cuts = (
            math.floor(len(groups) * policy["train_ratio"]),
            math.floor(len(groups) * (policy["train_ratio"] + policy["validation_ratio"])),
        )
    else:
        ordered_groups = Counter(r["event_epoch"] for r in members)
        targets = (
            len(members) * policy["train_ratio"],
            len(members) * (policy["train_ratio"] + policy["validation_ratio"]),
        )
        cumulative, cuts_list = 0, []
        for i, group in enumerate(groups):
            cumulative += ordered_groups[group]
            while len(cuts_list) < 2 and cumulative >= targets[len(cuts_list)]:
                cuts_list.append(i + 1)
        cuts = tuple(cuts_list)
    if len(cuts) != 2 or not 0 < cuts[0] < cuts[1] < len(groups):
        raise ResearchError("DATA_SPLIT_UNUSABLE")
    assigned = {
        g: PARTITIONS[0 if i < cuts[0] else 1 if i < cuts[1] else 2] for i, g in enumerate(groups)
    }
    counts = {p: Counter() for p in PARTITIONS}
    repositories, cohorts = defaultdict(Counter), defaultdict(Counter)
    for ordinal, member in enumerate(members):
        budget.check()
        member.update(ordinal=ordinal, partition=assigned[group_key(member)])
        counts[member["partition"]][member["label"]] += 1
        repositories[member["canonical_repository"]][member["label"]] += 1
        for cohort in member["cohorts"]:
            cohorts[cohort][member["label"]] += 1
    if any(not counts[p][0] or not counts[p][1] for p in PARTITIONS):
        raise ResearchError("DATA_SPLIT_SINGLE_CLASS")

    def summaries(groups):
        return {k: {"clean": v[0], "buggy": v[1]} for k, v in sorted(groups.items())}

    member_hash = content_hash(members)
    metadata = {
        "version": VERSION,
        "rule_hash": RULE_HASH,
        "numeric_encoding": "canonical-json-text-roundtrip-v1",
        "policy": policy,
        "source_hash": source_hash,
        "asset_id": manifest["asset_id"],
        "asset_quality_hash": quality["content_hash"],
        "source_acquisition": manifest["identity"]["source_acquisition"],
        "source_first_stored_at": manifest["first_stored_at"],
        "source_inventory": inventory,
        "rows": len(members),
        "original_rows": number,
        "duplicate_rows": duplicate_rows,
        "members_hash": member_hash,
        "partitions": summaries(counts),
        "repositories": summaries(repositories),
        "cohorts": summaries(cohorts),
        "features": list(NUMERIC_FIELDS),
        "target": "published_SZZ_binary_post_publication",
        "event_clock": "published_author_epoch_UTC",
        "capabilities": {
            "research_binary": True,
            "historical_replay": False,
            "candidate_release": False,
            "research_semantic": False,
            "type_prediction": False,
            "localization": False,
        },
        "limitations": [
            "published_weak_labels_not_gold",
            "not_historical_online_simulation",
            "no_independent_negative_coverage_certification",
            "no_language_or_type_certification",
            "no_preprocessing_fitted_or_models_trained",
            "cross_source_and_code_content_overlap_unverified",
        ],
    }
    budget.check()
    return members, metadata, content_hash(metadata)
