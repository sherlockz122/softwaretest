"""Predeclared, label-independent whole-line coverage probe; no model qualification."""

import json
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical, content_hash
from packages.semantic import admission, artifacts, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

BINS = ((1, 1), (2, 8), (9, 32), (33, 128), (129, 256))
QUOTAS = {"train": (10, 24, 20, 9, 1), "validation": (6, 14, 8, 3, 1)}
CODE_ROLES = ("added", "deleted", "base")
CONTENT_TOKENS = 1008
CODE_WINDOWS = 8
SOURCES = [
    "packages/semantic/coverage.py",
    "packages/semantic/coverage_encoder.py",
    "scripts/semantic_coverage_probe.py",
    "packages/semantic/admission.py",
    "packages/semantic/admission_git.py",
    "packages/semantic/encoder.py",
    "packages/semantic/policy.py",
    "packages/semantic/artifacts.py",
    "packages/semantic/control.py",
    "tools/semantic/uv.lock",
]


def probe_policy():
    return {
        "bins": [list(b) for b in BINS],
        "quotas": {k: list(v) for k, v in QUOTAS.items()},
        "code_tokens": CONTENT_TOKENS,
        "code_windows": CODE_WINDOWS,
        "message_windows": policy.WINDOWS,
        "tokens_per_window": policy.TOKENS,
        "budget_checks": admission.MAX_ATTEMPTS,
        "lines_per_hunk": admission.LINES_PER_HUNK,
        "maximum_hunks": admission.MAX_HUNKS,
        "preserve_v2_selected_lines": True,
        "baseline_budget_checks": admission.MAX_ATTEMPTS,
    }


def identity(row):
    member = row["member"]
    return {key: member[key] for key in ("canonical_repository", "partition", "commit_id")}


def stratum(hunks):
    if type(hunks) is not int:
        raise SemanticError("COVERAGE_HUNK_STRATUM")
    for index, (low, high) in enumerate(BINS):
        if low <= hunks <= high:
            return index
    raise SemanticError("COVERAGE_HUNK_STRATUM")


def selection(rows):
    """Only identity, partition and hunk count affect membership; never inspect labels."""
    seen, population, groups = set(), [], {}
    for row in rows:
        member = identity(row)
        part, sha = member["partition"], member["commit_id"]
        if part not in QUOTAS:
            raise SemanticError("COVERAGE_OLD_TEST_FORBIDDEN")
        if member["canonical_repository"] != policy.REPOSITORY:
            raise SemanticError("COVERAGE_REPOSITORY")
        if (
            not isinstance(sha, str)
            or len(sha) != 40
            or any(c not in "0123456789abcdef" for c in sha)
            or sha == "0" * 40
        ):
            raise SemanticError("COVERAGE_IDENTITY")
        key = member["canonical_repository"], sha
        if key in seen:
            raise SemanticError("COVERAGE_DUPLICATE_IDENTITY")
        seen.add(key)
        count = len(row["context"]["hunks"])
        index = stratum(count)
        record = member | {"hunks": count, "stratum": index}
        population.append(record)
        groups.setdefault((part, index), []).append(record)
    if Counter(p["partition"] for p in population) != {"train": 488, "validation": 172}:
        raise SemanticError("COVERAGE_POPULATION")
    chosen = []
    for part, quotas in QUOTAS.items():
        for index, quota in enumerate(quotas):
            candidates = sorted(groups.get((part, index), []), key=lambda p: p["commit_id"])
            if len(candidates) < quota:
                raise SemanticError("COVERAGE_STRATUM_SUPPORT")
            chosen.extend(candidates[:quota])
    return chosen, sorted(population, key=lambda p: (p["partition"], p["stratum"], p["commit_id"]))


def parent_rows(directory):
    manifest = artifacts.verify(directory, checkpoint)
    if manifest.get("version") != "hunk-context-v2" or manifest.get("accepted") != 660:
        raise SemanticError("COVERAGE_PARENT_VERSION")
    rows = list(artifacts.records(directory))
    if len(rows) != 660:
        raise SemanticError("COVERAGE_POPULATION")
    selection(rows)
    return manifest, rows


def safe_weights(directory):
    manifest = artifacts.verify(directory, checkpoint)
    if (
        manifest.get("safe_format") != "safetensors"
        or manifest.get("weights_only") is not True
        or manifest.get("original", {}).get("revision") != policy.REVISION
    ):
        raise SemanticError("COVERAGE_UNSAFE_CHECKPOINT")
    return manifest


def declare(inputs, checkpoint_dir, output):
    inputs, checkpoint_dir, output = [
        assets.safe_path(Path(p)) for p in (inputs, checkpoint_dir, output)
    ]
    executed_sources = artifacts.sources(SOURCES)
    parent, rows = parent_rows(inputs)
    pretrained = safe_weights(checkpoint_dir)
    chosen, population = selection(rows)
    impact = {
        part: {
            "population_bins": [
                sum(p["partition"] == part and p["stratum"] == i for p in population)
                for i in range(5)
            ],
            "selected_bins": list(QUOTAS[part]),
            "population_records_with_unrepresented_hunks": sum(
                r["member"]["partition"] == part
                and any(r["context"]["roles"][role]["unrepresented_hunks"] for role in CODE_ROLES)
                for r in rows
            ),
        }
        for part in QUOTAS
    }
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".selection-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(attempt / "selection.json", {"members": chosen})
    assets.write_json(attempt / "population.json", {"members": population, "impact": impact})
    if (
        artifacts.verify(inputs, checkpoint) != parent
        or artifacts.verify(checkpoint_dir, checkpoint) != pretrained
        or artifacts.sources(SOURCES) != executed_sources
    ):
        raise SemanticError("COVERAGE_DECLARATION_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "semantic-coverage-declaration-v1",
            "accepted": len(chosen),
            "parent": parent["content_hash"],
            "checkpoint": pretrained["content_hash"],
            "parent_directory": inputs.relative_to(assets.ROOT).as_posix(),
            "checkpoint_directory": checkpoint_dir.relative_to(assets.ROOT).as_posix(),
            "source": parent["source"],
            "sources": executed_sources,
            "policy": probe_policy(),
            "label_independent": True,
            "candidate_release": False,
        },
        ["selection.json", "population.json"],
    )


def joined_lines(selected):
    return "\n".join(
        "".join(value for (fi, _), value in sorted(selected.items()) if fi == file)
        for file in sorted({fi for fi, _ in selected})
    )


def select_lines(groups, encode, *, budget=CONTENT_TOKENS, check=checkpoint, baseline=None):
    if type(budget) is not int or not 0 < budget <= CONTENT_TOKENS:
        raise SemanticError("COVERAGE_TOKEN_BUDGET")
    selected, attempts = {}, 0
    if budget > admission.CONTENT_TOKENS:
        if baseline is None:
            _, old_audit = admission.select_lines(groups, encode, check=check)
            baseline = old_audit["selected_coordinates"]
        available = {(fi, line): body for g in groups for fi, line, body in g["candidates"]}
        for coordinate in baseline:
            key = coordinate["file"], coordinate["line"] - 1
            if key not in available or key in selected:
                raise SemanticError("COVERAGE_BASELINE_SEED_DRIFT")
            selected[key] = available[key]
        if len(encode(joined_lines(selected))) > budget:
            raise SemanticError("COVERAGE_BASELINE_SEED_BUDGET")
    for ordinal in range(max((len(g["candidates"]) for g in groups), default=0)):
        for group in groups:
            if ordinal >= len(group["candidates"]):
                continue
            fi, line, body = group["candidates"][ordinal]
            key = fi, line
            if key in selected:
                continue
            if attempts >= admission.MAX_ATTEMPTS:
                break
            check()
            attempts += 1
            candidate = selected | {key: body}
            if len(encode(joined_lines(candidate))) <= budget:
                selected = candidate
        if attempts >= admission.MAX_ATTEMPTS:
            break
    value = joined_lines(selected)
    represented = sum(
        any((fi, line) in selected for fi, line, _ in g["candidates"]) for g in groups
    )
    return value, {
        "selected_coordinates": [{"file": fi, "line": line + 1} for fi, line in sorted(selected)],
        "selected_tokens": len(encode(value)),
        "candidate_hunks": len(groups),
        "represented_hunks": represented,
        "unrepresented_hunks": len(groups) - represented,
        "budget_checks": attempts,
        "candidate_lines_before_cap": sum(g["eligible_lines"] for g in groups),
        "candidate_lines_after_cap": sum(len(g["candidates"]) for g in groups),
    }


def line_coverage(groups, audit):
    selected = {(c["file"], c["line"] - 1) for c in audit["selected_coordinates"]}
    incidences = [sum((fi, line) in selected for fi, line, _ in g["candidates"]) for g in groups]
    return audit | {
        "selected_hunk_line_incidences": sum(incidences),
        "complete_eligible_hunks": sum(
            n == g["eligible_lines"] for n, g in zip(incidences, groups, strict=True)
        ),
        "candidate_cap_omitted_lines": audit["candidate_lines_before_cap"]
        - audit["candidate_lines_after_cap"],
        "line_denominator": "hunk-line incidences; shared base lines may count for several hunks",
    }


def transform(row, reader, encode):
    if row["member"]["partition"] not in QUOTAS:
        raise SemanticError("COVERAGE_OLD_TEST_FORBIDDEN")
    if not 0 < len(row["files"]) <= policy.MAX_FILES:
        raise SemanticError("COVERAGE_FILE_LIMIT")
    reader.deadline = time.monotonic() + 30
    files = [
        (
            admission.blob_text(reader, f["old_blob"], f["before_bytes"]),
            admission.blob_text(reader, f["new_blob"], f["after_bytes"]),
        )
        for f in row["files"]
    ]
    groups, hunks = admission.hunk_groups(files)
    if hunks != row["context"]["hunks"] or len(hunks) > admission.MAX_HUNKS:
        raise SemanticError("COVERAGE_HUNK_DRIFT")
    roles, audit, baseline = {"message": row["roles"]["message"]}, {}, {}
    if content_hash(row["roles"]) != row["text_hash"]:
        raise SemanticError("COVERAGE_PARENT_TEXT_DRIFT")
    for role in CODE_ROLES:
        old_text, old_audit = admission.select_lines(groups[role], encode)
        if old_text != row["roles"][role] or any(
            row["context"]["roles"][role][k] != v for k, v in old_audit.items()
        ):
            raise SemanticError("COVERAGE_V2_RECONSTRUCTION_DRIFT")
        roles[role], current = select_lines(
            groups[role], encode, baseline=old_audit["selected_coordinates"]
        )
        baseline[role] = line_coverage(groups[role], old_audit)
        audit[role] = line_coverage(groups[role], current)
        old_points = {(p["file"], p["line"]) for p in old_audit["selected_coordinates"]}
        new_points = {(p["file"], p["line"]) for p in current["selected_coordinates"]}
        if not old_points <= new_points:
            raise SemanticError("COVERAGE_BASELINE_LINE_LOSS")
        audit[role]["v2_selected_lines_preserved"] = True
    return {
        "member": identity(row),
        "roles": roles,
        "text_hash": content_hash(roles),
        "code_hash": content_hash({k: v for k, v in roles.items() if k != "message"}),
        "context": {
            "version": "hunk-context-v3-bounded-probe",
            "hunks": hunks,
            "v2_text_hash": row["text_hash"],
            "v2_reconstruction_exact": True,
            "baseline_roles": baseline,
            "roles": audit,
            "full_commit_encoded": False,
        },
    }


def contexts(inputs, declaration, source, checkpoint_dir, output, encode=None):
    inputs, declaration, source, checkpoint_dir, output = [
        assets.safe_path(Path(p)) for p in (inputs, declaration, source, checkpoint_dir, output)
    ]
    parent, rows = parent_rows(inputs)
    predeclared = artifacts.verify(declaration, checkpoint)
    pretrained = safe_weights(checkpoint_dir)
    if (
        predeclared.get("version") != "semantic-coverage-declaration-v1"
        or predeclared.get("parent") != parent["content_hash"]
        or predeclared.get("checkpoint") != pretrained["content_hash"]
        or predeclared.get("policy") != probe_policy()
        or predeclared.get("source") != parent["source"]
        or predeclared.get("sources") != artifacts.sources(SOURCES)
    ):
        raise SemanticError("COVERAGE_DECLARATION_DRIFT")
    chosen, population = selection(rows)
    if (
        assets.read_json(declaration / "selection.json", checkpoint)["members"] != chosen
        or assets.read_json(declaration / "population.json", checkpoint)["members"] != population
    ):
        raise SemanticError("COVERAGE_SELECTION_DRIFT")
    if encode is None:
        from transformers import RobertaTokenizer

        tokenizer = RobertaTokenizer.from_pretrained(checkpoint_dir, local_files_only=True)
        tokenizer.model_max_length = 10**12

        @lru_cache(maxsize=4096)
        def cached(value):
            return tuple(tokenizer.encode(value, add_special_tokens=False))

        def encode(value):
            return (
                cached(value)
                if len(value) <= 4096
                else tokenizer.encode(value, add_special_tokens=False)
            )

    if any(
        (source / "source.git" / p).exists()
        for p in (
            "shallow",
            "objects/info/alternates",
            "objects/info/http-alternates",
            "info/grafts",
        )
    ):
        raise SemanticError("COVERAGE_UNSAFE_SOURCE")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".contexts-" + uuid4().hex)
    attempt.mkdir()
    by_sha = {r["member"]["commit_id"]: r for r in rows}
    transformed = []
    with TemporaryDirectory(dir=attempt) as temporary:
        reader, receipt = admission.source_reader(source, Path(temporary))
        if receipt != parent["source"]:
            raise SemanticError("COVERAGE_SOURCE_DRIFT")
        for member in chosen:
            checkpoint()
            transformed.append(transform(by_sha[member["commit_id"]], reader, encode))
            if len(transformed) % 8 == 0:
                print(
                    json.dumps({"contexts_completed": len(transformed), "total": len(chosen)}),
                    flush=True,
                )
    with (attempt / "records.jsonl").open("xb") as stream:
        for row in transformed:
            stream.write(canonical(row) + b"\n")
    (attempt / "licenses.json").write_bytes((inputs / "licenses.json").read_bytes())
    summary = {}
    for name in ("baseline_roles", "roles"):
        summary[name] = {
            role: {
                field: sum(r["context"][name][role][field] for r in transformed)
                for field in (
                    "selected_tokens",
                    "candidate_hunks",
                    "represented_hunks",
                    "unrepresented_hunks",
                    "candidate_lines_before_cap",
                    "candidate_lines_after_cap",
                    "selected_hunk_line_incidences",
                    "complete_eligible_hunks",
                    "candidate_cap_omitted_lines",
                )
            }
            for role in CODE_ROLES
        }
    summary.update(
        selected_records=len(transformed),
        full_population_coverage_certified=False,
        representation="one whole candidate line; base may be unchanged; not full-change coverage",
        records_still_unrepresented=sum(
            any(r["context"]["roles"][role]["unrepresented_hunks"] for role in CODE_ROLES)
            for r in transformed
        ),
        member_labels_used=False,
        new_classifier_fitted=False,
        old_test_transformed=False,
        candidate_release=False,
    )
    assets.write_json(attempt / "audit.json", summary)
    if (
        artifacts.verify(inputs, checkpoint) != parent
        or artifacts.verify(declaration, checkpoint) != predeclared
        or artifacts.verify(checkpoint_dir, checkpoint) != pretrained
        or artifacts.sources(SOURCES) != predeclared["sources"]
    ):
        raise SemanticError("COVERAGE_INPUT_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "hunk-context-v3-bounded-probe",
            "accepted": len(transformed),
            "declaration": predeclared["content_hash"],
            "parent": parent["content_hash"],
            "checkpoint": pretrained["content_hash"],
            "parent_directory": inputs.relative_to(assets.ROOT).as_posix(),
            "declaration_directory": declaration.relative_to(assets.ROOT).as_posix(),
            "checkpoint_directory": checkpoint_dir.relative_to(assets.ROOT).as_posix(),
            "source": receipt,
            "sources": artifacts.sources(SOURCES),
            "policy": predeclared["policy"],
            "candidate_release": False,
            "new_classifier_fitted": False,
            "old_test_transformed": False,
            "old_model_compatible": False,
        },
        ["records.jsonl", "audit.json", "licenses.json"],
    )
