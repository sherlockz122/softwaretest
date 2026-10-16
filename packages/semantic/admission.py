"""Traceable train/validation contexts and an unlabeled future reservation, never a release."""

import csv
import difflib
import time
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.research_dataset import CSV_FIELDS, PARTITIONS
from packages.mining.source_evidence import canonical, content_hash
from packages.semantic import artifacts, policy
from packages.semantic.admission_git import (
    NativeReader,
    native_environment,
    object_hash,
    sha_valid,
    text,
)
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

CONTENT_TOKENS = 252
RADIUS = 3
MAX_HUNKS = 256
LINES_PER_HUNK = 32
MAX_ATTEMPTS = 512
START = 1735689600  # 2025-01-01T00:00:00Z, committer clock
STOP = 1751328000  # 2025-07-01T00:00:00Z, exclusive


def source_reader(source, home):
    receipt = assets.read_json(source / "acquisition.json", checkpoint)
    assets.verify_seal(receipt)
    if receipt["repository"] != policy.REPOSITORY:
        raise SemanticError("ADMISSION_REPOSITORY_CONFLICT")
    bare = source / "source.git"
    if any(
        (bare / p).exists()
        for p in ["objects/info/alternates", "objects/info/http-alternates", "info/grafts"]
    ):
        raise SemanticError("ADMISSION_ALTERNATES")
    reader = NativeReader(
        bare, home, native_environment(home, bare), checkpoint, time.monotonic() + 30
    )
    if reader.command(["rev-parse", "--is-bare-repository"]).strip() != b"true":
        raise SemanticError("ADMISSION_STORAGE_INVALID")
    if (
        reader.command(["rev-parse", receipt["head"] + "^{commit}"]).strip().decode()
        != receipt["head"]
    ):
        raise SemanticError("ADMISSION_HEAD_CONFLICT")
    return reader, receipt


def blob_text(reader, identity, expected):
    sha_valid(identity.encode())
    if identity == "0" * 40:
        body = b""
    else:
        body = reader.command(["cat-file", "blob", identity], limit=policy.MAX_BLOB)
        if object_hash("blob", body) != identity:
            raise SemanticError("ADMISSION_BLOB_DRIFT")
    if len(body) != expected:
        raise SemanticError("ADMISSION_BLOB_LENGTH_DRIFT")
    return text(body)


def hunk_groups(files):
    """Coordinates are zero-based internally, exported one-based, original CRLF preserved."""
    groups = {r: [] for r in ["added", "deleted", "base"]}
    all_hunks = []
    for fi, (before, after) in enumerate(files):

        def git_lines(value):
            parts = value.split("\n")
            return [line + "\n" for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])

        old, new = git_lines(before), git_lines(after)
        for tag, a, b, c, d in difflib.SequenceMatcher(
            None, old, new, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                continue
            hi = len(all_hunks)
            all_hunks.append({"file": fi, "id": hi, "old": [a, b], "new": [c, d], "tag": tag})
            if hi >= MAX_HUNKS:
                continue
            ranges = {"added": list(range(c, d)), "deleted": list(range(a, b))}
            anchor = min(a, max(0, len(old) - 1))
            base = list(range(max(0, a - RADIUS), min(len(old), max(a + 1, b) + RADIUS)))
            ranges["base"] = sorted(
                base, key=lambda i: (0 if a <= i < b else 1, abs(i - anchor), i)
            )
            for role, indices in ranges.items():
                lines = new if role == "added" else old
                if indices:
                    groups[role].append(
                        {
                            "hunk": hi,
                            "candidates": [(fi, i, lines[i]) for i in indices[:LINES_PER_HUNK]],
                            "eligible_lines": len(indices),
                        }
                    )
        checkpoint()
    return groups, all_hunks


def select_lines(groups, encode, check=checkpoint):
    selected, attempts = {}, 0
    maximum = max((len(g["candidates"]) for g in groups), default=0)
    for ordinal in range(maximum):
        for group in groups:
            if ordinal >= len(group["candidates"]):
                continue
            fi, line, body = group["candidates"][ordinal]
            key = fi, line
            if key in selected:
                continue
            if attempts >= MAX_ATTEMPTS:
                break
            check()
            attempts += 1
            candidate = selected | {key: body}
            joined = "\n".join(
                "".join(v for (f, _), v in sorted(candidate.items()) if f == file)
                for file in sorted({f for f, _ in candidate})
            )
            if len(encode(joined)) <= CONTENT_TOKENS:
                selected = candidate
        if attempts >= MAX_ATTEMPTS:
            break
    joined = "\n".join(
        "".join(v for (f, _), v in sorted(selected.items()) if f == file)
        for file in sorted({f for f, _ in selected})
    )
    coordinates = [{"file": fi, "line": line + 1} for fi, line in sorted(selected)]
    represented = sum(
        any((fi, line) in selected for fi, line, _ in g["candidates"]) for g in groups
    )
    return joined, {
        "selected_coordinates": coordinates,
        "selected_tokens": len(encode(joined)),
        "candidate_hunks": len(groups),
        "represented_hunks": represented,
        "unrepresented_hunks": len(groups) - represented,
        "budget_checks": attempts,
        "candidate_lines_before_cap": sum(g["eligible_lines"] for g in groups),
        "candidate_lines_after_cap": sum(len(g["candidates"]) for g in groups),
    }


def transform(row, reader, encode):
    if row["member"]["partition"] not in {"train", "validation"}:
        raise SemanticError("ADMISSION_OLD_TEST_FORBIDDEN")
    reader.deadline = time.monotonic() + 30
    files = [
        (
            blob_text(reader, f["old_blob"], f["before_bytes"]),
            blob_text(reader, f["new_blob"], f["after_bytes"]),
        )
        for f in row["files"]
    ]
    # Verify this is precisely the previously frozen base, not an arbitrary new source.
    if "\n".join(before for before, _ in files) != row["roles"]["base"]:
        raise SemanticError("ADMISSION_BASE_BINDING_DRIFT")
    groups, hunks = hunk_groups(files)
    roles, audit = {"message": row["roles"]["message"]}, {}
    for role in ["added", "deleted", "base"]:
        roles[role], audit[role] = select_lines(groups[role], encode)
        audit[role]["original_role_tokens"] = len(encode(row["roles"][role]))
    return row | {
        "roles": roles,
        "text_hash": content_hash(roles),
        "code_hash": content_hash({k: v for k, v in roles.items() if k != "message"}),
        "context": {
            "version": "hunk-context-v2",
            "hunks": hunks,
            "hunks_beyond_cap": max(0, len(hunks) - MAX_HUNKS),
            "roles": audit,
            "full_commit_encoded": False,
            "original_text_hash": row["text_hash"],
        },
    }


def support_audit(rows):
    import numpy as np

    counts = {
        p: dict(Counter(r["member"]["label"] for r in rows if r["member"]["partition"] == p))
        for p in ["train", "validation"]
    }
    validation = [r["member"] for r in rows if r["member"]["partition"] == "validation"]
    epochs = np.array([int(r["event_epoch"]) for r in validation])
    labels = np.array([int(r["label"]) for r in validation])
    # Frozen time-group rule, independently implemented without importing skops/model fitting.
    unique = np.unique(epochs)
    if len(unique) < 2:
        raise SemanticError("ADMISSION_CALIBRATION_UNUSABLE")
    boundary = unique[len(unique) // 2]
    first, second = epochs < boundary, epochs >= boundary
    if any(set(labels[mask]) != {0, 1} for mask in [first, second]):
        raise SemanticError("ADMISSION_CALIBRATION_UNUSABLE")
    for name, mask in [("calibration", first), ("threshold", second)]:
        counts[name] = {"0": int((labels[mask] == 0).sum()), "1": int((labels[mask] == 1).sum())}
    return {
        "counts": counts,
        "minimum_positive_floor": 10,
        "calibration_and_threshold_floor_met": all(
            counts[p]["1"] >= 10 for p in ["calibration", "threshold"]
        ),
        "floor_is_statistical_adequacy_certification": False,
        "resplit_performed": False,
    }


def contexts(inputs, source, checkpoint_dir, output, encode=None):
    inputs, source, checkpoint_dir, output = [
        assets.safe_path(Path(p)) for p in [inputs, source, checkpoint_dir, output]
    ]
    original, pretrained = (
        artifacts.verify(inputs, checkpoint),
        artifacts.verify(checkpoint_dir, checkpoint),
    )
    if (
        pretrained.get("safe_format") != "safetensors"
        or pretrained.get("weights_only") is not True
        or pretrained["original"]["revision"] != policy.REVISION
    ):
        raise SemanticError("ADMISSION_CHECKPOINT_CONFLICT")
    if encode is None:
        from transformers import RobertaTokenizer

        tokenizer = RobertaTokenizer.from_pretrained(checkpoint_dir, local_files_only=True)
        tokenizer.model_max_length = 10**12

        @lru_cache(maxsize=4096)
        def cached_ids(s):
            return tuple(tokenizer.encode(s, add_special_tokens=False))

        def encode(s):
            return (
                cached_ids(s) if len(s) <= 4096 else tokenizer.encode(s, add_special_tokens=False)
            )

    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".contexts-" + uuid4().hex)
    attempt.mkdir()
    rows = []
    with TemporaryDirectory(dir=attempt) as work:
        reader, receipt = source_reader(source, Path(work))
        if receipt != original["source"]:
            raise SemanticError("ADMISSION_SOURCE_BINDING_DRIFT")
        for row in artifacts.records(inputs):
            checkpoint()
            if row["member"]["partition"] not in {"train", "validation"}:
                continue
            rows.append(transform(row, reader, encode))
            if len(rows) % 50 == 0:
                print(__import__("json").dumps({"contexts_completed": len(rows)}), flush=True)
    if not rows:
        raise SemanticError("ADMISSION_NO_TRAIN_VALIDATION")
    with (attempt / "records.jsonl").open("xb") as stream:
        for row in rows:
            stream.write(canonical(row) + b"\n")
    (attempt / "licenses.json").write_bytes((inputs / "licenses.json").read_bytes())
    summary = {
        role: {
            name: sum(r["context"]["roles"][role][name] for r in rows)
            for name in [
                "original_role_tokens",
                "selected_tokens",
                "candidate_hunks",
                "represented_hunks",
                "unrepresented_hunks",
                "candidate_lines_before_cap",
                "candidate_lines_after_cap",
            ]
        }
        for role in ["added", "deleted", "base"]
    }
    duplicate_groups = {}
    for row in rows:
        for kind in ["text_hash", "code_hash"]:
            duplicate_groups.setdefault((kind, row[kind]), set()).add(row["member"]["partition"])
    duplicates = sum(len(parts) > 1 for parts in duplicate_groups.values())
    assets.write_json(
        attempt / "audit.json",
        {
            "roles": summary,
            "support": support_audit(rows),
            "full_commit_encoded": False,
            "hunks_beyond_cap": sum(r["context"]["hunks_beyond_cap"] for r in rows),
            "cross_split_exact_duplicate_groups": duplicates,
            "represented_hunk_definition": (
                "one selected candidate line; base may be unchanged context; "
                "not full change coverage"
            ),
        },
    )
    if (
        artifacts.verify(inputs, checkpoint) != original
        or artifacts.verify(checkpoint_dir, checkpoint) != pretrained
    ):
        raise SemanticError("ADMISSION_INPUT_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "hunk-context-v2",
            "accepted": len(rows),
            "original_inputs": original["content_hash"],
            "checkpoint_manifest": pretrained["content_hash"],
            "source": receipt,
            "policy": {
                "tokens_per_role": CONTENT_TOKENS,
                "radius": RADIUS,
                "maximum_hunks": MAX_HUNKS,
                "lines_per_hunk": LINES_PER_HUNK,
                "budget_checks_per_role": MAX_ATTEMPTS,
                "line_coordinates": (
                    "Git LF only; zero-based half-open hunks, one-based selected lines"
                ),
            },
            "supports": support_audit(rows),
            "old_test_transformed": False,
            "candidate_release": False,
            "new_classifier_fitted": False,
            "sources": artifacts.sources(
                [
                    "packages/semantic/admission.py",
                    "scripts/semantic_admission.py",
                    "packages/semantic/admission_git.py",
                    "packages/semantic/artifacts.py",
                    "packages/semantic/control.py",
                    "tools/semantic/uv.lock",
                ]
            ),
        },
        ["records.jsonl", "licenses.json", "audit.json"],
    )


def excluded_members(dataset):
    manifest = assets.read_json(dataset / "manifest.json", checkpoint)
    assets.verify_seal(manifest)
    members = set()
    for part in PARTITIONS:
        path = dataset / (part + ".csv")
        if artifacts.file_digest(path, checkpoint) != manifest["files"][part]:
            raise SemanticError("ADMISSION_DATASET_DRIFT")
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise SemanticError("ADMISSION_DATASET_SCHEMA")
            members.update((r["canonical_repository"], r["commit_id"]) for r in reader)
    return members, manifest


def commit_clocks(raw, sha):
    if object_hash("commit", raw) != sha:
        raise SemanticError("ADMISSION_COMMIT_DRIFT")
    headers = raw.split(b"\n\n", 1)[0].splitlines()
    values = {
        kind: [line[len(kind) + 1 :] for line in headers if line.startswith(kind + b" ")]
        for kind in [b"author", b"committer", b"parent"]
    }
    if len(values[b"author"]) != 1 or len(values[b"committer"]) != 1:
        raise SemanticError("ADMISSION_CLOCK_INVALID")
    return {
        "commit_id": sha,
        "author_epoch": int(values[b"author"][0].rsplit(b" ", 2)[1]),
        "committer_epoch": int(values[b"committer"][0].rsplit(b" ", 2)[1]),
        "parents": [sha_valid(v) for v in values[b"parent"]],
    }


def choose_reservation(records, excluded, start=START, stop=STOP, limit=64):
    if type(limit) is not int or not 1 <= limit <= 64:
        raise SemanticError("ADMISSION_RESERVATION_LIMIT")
    eligible = [
        r
        for r in records
        if start <= r["committer_epoch"] < stop
        and len(r["parents"]) == 1
        and (policy.REPOSITORY, r["commit_id"]) not in excluded
    ]
    unique = {r["commit_id"]: r for r in eligible}
    if len(unique) != len(eligible):
        raise SemanticError("ADMISSION_DUPLICATE_COMMIT")
    return [unique[k] for k in sorted(unique)[:limit]]


def reserve(dataset, source, output):
    dataset, source, output = [assets.safe_path(Path(p)) for p in [dataset, source, output]]
    excluded, frozen = excluded_members(dataset)
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".reservation-" + uuid4().hex)
    attempt.mkdir()
    records = []
    with TemporaryDirectory(dir=attempt) as work:
        reader, receipt = source_reader(source, Path(work))
        raw = reader.command(["rev-list", "--timestamp", receipt["head"], "--"], limit=4 * 1024**2)
        candidates = [
            (int(line.split()[0]), sha_valid(line.split()[1])) for line in raw.splitlines()
        ]
        window = [
            (epoch, sha)
            for epoch, sha in candidates
            if START <= epoch < STOP and (policy.REPOSITORY, sha) not in excluded
        ]
        if len(window) > 2048:
            raise SemanticError("ADMISSION_RESERVATION_WINDOW_LIMIT")
        for epoch, sha in window:
            checkpoint()
            reader.deadline = time.monotonic() + 30
            value = commit_clocks(reader.command(["cat-file", "commit", sha], limit=65536), sha)
            if value["committer_epoch"] != epoch:
                raise SemanticError("ADMISSION_REVLIST_CLOCK_DRIFT")
            records.append(value)
    selected = choose_reservation(records, excluded, start=START, stop=STOP)
    if not selected:
        raise SemanticError("ADMISSION_NO_FUTURE_CANDIDATES")
    assets.write_json(
        attempt / "reservation.json",
        {"records": selected, "labels": "not_collected", "predictions": "not_computed"},
    )
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "unlabeled-future-reservation-v1",
            "source": receipt,
            "frozen_dataset": frozen["content_hash"],
            "excluded_member_count": len(excluded),
            "window": [START, STOP],
            "eligible_single_parent": sum(len(r["parents"]) == 1 for r in records),
            "inspected": len(window),
            "reserved": len(selected),
            "selection": "smallest SHA after fixed clock/parent/member checks; no labels",
            "holdout_ready": False,
            "candidate_release": False,
            "observed_at": datetime.now(UTC).isoformat(),
            "missing_conditions": [
                "independent mature labels and clean coverage",
                "class and calibration support",
                "near duplicate and pretraining overlap audit",
                "applicable license evidence",
            ],
            "sources": artifacts.sources(
                [
                    "packages/semantic/admission.py",
                    "scripts/semantic_admission.py",
                    "packages/semantic/admission_git.py",
                ]
            ),
        },
        ["reservation.json"],
    )
