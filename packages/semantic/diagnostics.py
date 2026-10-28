"""Fixed residual explanations; local token costs are never additive infeasibility proofs."""

import json
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical
from packages.semantic import admission, artifacts, coverage, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

SOURCES = [
    *coverage.SOURCES,
    "packages/semantic/diagnostics.py",
    "scripts/semantic_residual_probe.py",
]


def incomplete(row):
    return any(row["context"]["roles"][r]["unrepresented_hunks"] for r in coverage.CODE_ROLES)


def choose(rows):
    if len(rows) != 96:
        raise SemanticError("DIAGNOSIS_POPULATION")
    members, seen = [], set()
    for row in rows:
        member = coverage.identity(row)
        sha = member["commit_id"]
        if (
            member["partition"] not in coverage.QUOTAS
            or member["canonical_repository"] != policy.REPOSITORY
            or not isinstance(sha, str)
            or len(sha) != 40
            or any(c not in "0123456789abcdef" for c in sha)
            or sha == "0" * 40
            or sha in seen
        ):
            raise SemanticError("DIAGNOSIS_IDENTITY")
        seen.add(sha)
        members.append(
            member
            | {
                "stratum": coverage.stratum(len(row["context"]["hunks"])),
                "residual": bool(incomplete(row)),
            }
        )
    if sum(m["residual"] for m in members) != 7:
        raise SemanticError("DIAGNOSIS_RESIDUAL_COUNT")
    if Counter((m["partition"], m["stratum"]) for m in members) != {
        (part, i): n for part, quotas in coverage.QUOTAS.items() for i, n in enumerate(quotas)
    }:
        raise SemanticError("DIAGNOSIS_STRATA")
    chosen = [m | {"selection_reason": "all_residuals"} for m in members if m["residual"]]
    missing = []
    for part in coverage.QUOTAS:
        for index in range(5):
            candidates = sorted(
                (
                    m
                    for m in members
                    if m["partition"] == part and m["stratum"] == index and not m["residual"]
                ),
                key=lambda m: m["commit_id"],
            )
            if candidates:
                chosen.append(candidates[0] | {"selection_reason": "first_complete_sha_control"})
            else:
                missing.append({"partition": part, "stratum": index})
    return sorted(chosen, key=lambda m: (m["partition"], m["commit_id"])), missing


def declare(inputs, output):
    inputs, output = [assets.safe_path(p) for p in (inputs, output)]
    sources = artifacts.sources(SOURCES)
    manifest = artifacts.verify(inputs, checkpoint)
    if (
        manifest.get("version") != "hunk-context-v3-bounded-probe"
        or manifest.get("policy") != coverage.probe_policy()
        or manifest.get("sources") != artifacts.sources(coverage.SOURCES)
    ):
        raise SemanticError("DIAGNOSIS_PARENT")
    members, missing = choose(list(artifacts.records(inputs)))
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".declaration-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(attempt / "selection.json", {"members": members, "missing_controls": missing})
    if artifacts.verify(inputs, checkpoint) != manifest or artifacts.sources(SOURCES) != sources:
        raise SemanticError("DIAGNOSIS_DECLARATION_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "semantic-residual-declaration-v1",
            "accepted": len(members),
            "parent": manifest["content_hash"],
            "input_directory": inputs.relative_to(assets.ROOT).as_posix(),
            "sources": sources,
            "candidate_release": False,
            "policy": {
                "code_tokens": 1008,
                "replay_checks": "all finite candidate incidences",
                "witness": (
                    "preserve v2 floor; smallest isolated-token whole line per uncovered hunk; "
                    "coordinate ties"
                ),
                "infeasibility_certified": False,
                "classifier_fit": False,
            },
        },
        ["selection.json"],
    )


def points(audit):
    return {(p["file"], p["line"] - 1) for p in audit["selected_coordinates"]}


def represented(groups, selected):
    return [
        g["hunk"] for g in groups if any((f, line) in selected for f, line, _ in g["candidates"])
    ]


def replay(groups, floor, encode, limit):
    """Independent replay changes only the check count, never line/token geometry."""
    selected, checks = dict(floor), 0
    for ordinal in range(max((len(g["candidates"]) for g in groups), default=0)):
        for group in groups:
            if ordinal >= len(group["candidates"]):
                continue
            fi, line, body = group["candidates"][ordinal]
            if (fi, line) in selected:
                continue
            if checks >= limit:
                return selected, checks
            checkpoint()
            checks += 1
            proposal = selected | {(fi, line): body}
            if len(encode(coverage.joined_lines(proposal))) <= 1008:
                selected = proposal
    return selected, checks


def role_diagnosis(groups, old, current, encode):
    if len(groups) > 256 or any(not g["candidates"] or len(g["candidates"]) > 32 for g in groups):
        raise SemanticError("DIAGNOSIS_GEOMETRY")
    available = {}
    for group in groups:
        for fi, line, body in group["candidates"]:
            if (fi, line) in available and available[fi, line] != body:
                raise SemanticError("DIAGNOSIS_SHARED_BODY_DRIFT")
            available[fi, line] = body
    old_points, selected = points(old), points(current)
    if not old_points <= selected <= available.keys():
        raise SemanticError("DIAGNOSIS_FLOOR")
    floor = {k: available[k] for k in old_points}
    same, checks = replay(groups, floor, encode, 512)
    if (
        set(same) != selected
        or checks != current["budget_checks"]
        or len(encode(coverage.joined_lines(same))) != current["selected_tokens"]
    ):
        raise SemanticError("DIAGNOSIS_REPLAY_DRIFT")
    finite = sum(len(g["candidates"]) for g in groups)
    uncapped, uncapped_checks = replay(groups, floor, encode, finite)
    costs = {}
    for key, body in sorted(available.items()):
        checkpoint()
        costs[key] = len(encode(body))
    witness = dict(floor)
    for group in sorted(groups, key=lambda g: g["hunk"]):
        if not any((f, line) in witness for f, line, _ in group["candidates"]):
            key = min(
                ((f, line) for f, line, _ in group["candidates"]), key=lambda k: (costs[k], *k)
            )
            witness[key] = available[key]
    represented_now = set(represented(groups, selected))
    local = [
        {
            "hunk": g["hunk"],
            "represented_v3": g["hunk"] in represented_now,
            "candidate_lines": len(g["candidates"]),
            "eligible_lines": g["eligible_lines"],
            "minimum_isolated_whole_line_tokens": min(
                costs[f, line] for f, line, _ in g["candidates"]
            ),
        }
        for g in groups
    ]
    witness_tokens = len(encode(coverage.joined_lines(witness)))
    return {
        "local_hunks": local,
        "unique_candidate_lines": len(available),
        "shared_candidate_incidences": finite - len(available),
        "candidate_cap_omitted_incidences": sum(g["eligible_lines"] for g in groups) - finite,
        "floor_tokens": len(encode(coverage.joined_lines(floor))),
        "floor_coordinates": old["selected_coordinates"],
        "v3_tokens": current["selected_tokens"],
        "v3_checks": checks,
        "v3_unrepresented": [g["hunk"] for g in groups if g["hunk"] not in represented_now],
        "finite_replay_checks": uncapped_checks,
        "finite_replay_unrepresented": [
            g["hunk"] for g in groups if g["hunk"] not in represented(groups, uncapped)
        ],
        "finite_replay_same_selection": set(uncapped) == selected,
        "witness_coordinates": [{"file": f, "line": line + 1} for f, line in sorted(witness)],
        "witness_tokens": witness_tokens,
        "witness_all_candidate_hunks_represented": len(represented(groups, witness)) == len(groups),
        "witness_within_1008": witness_tokens <= 1008,
        "joined_minimum_or_infeasibility_certified": False,
        "local_costs_additive": False,
    }


def diagnose(inputs, declaration, source, checkpoint_dir, output, encode=None):
    inputs, declaration, source, checkpoint_dir, output = [
        assets.safe_path(p) for p in (inputs, declaration, source, checkpoint_dir, output)
    ]
    parent, declared = (
        artifacts.verify(inputs, checkpoint),
        artifacts.verify(declaration, checkpoint),
    )
    sources = artifacts.sources(SOURCES)
    if (
        declared.get("version") != "semantic-residual-declaration-v1"
        or declared.get("sources") != sources
        or declared.get("parent") != parent["content_hash"]
    ):
        raise SemanticError("DIAGNOSIS_DECLARATION_DRIFT")
    rows = list(artifacts.records(inputs))
    members, missing = choose(rows)
    if assets.read_json(declaration / "selection.json", checkpoint) != {
        "members": members,
        "missing_controls": missing,
    }:
        raise SemanticError("DIAGNOSIS_MEMBERS_DRIFT")
    old_dir = assets.safe_path(assets.ROOT / parent["parent_directory"])
    old_manifest, old_rows = coverage.parent_rows(old_dir)
    pretrained = coverage.safe_weights(checkpoint_dir)
    if (
        old_manifest["content_hash"] != parent["parent"]
        or pretrained["content_hash"] != parent["checkpoint"]
    ):
        raise SemanticError("DIAGNOSIS_DEPENDENCY_DRIFT")
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

    original = {r["member"]["commit_id"]: r for r in old_rows}
    current = {r["member"]["commit_id"]: r for r in rows}
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".diagnosis-" + uuid4().hex)
    attempt.mkdir()
    results = []
    with TemporaryDirectory(dir=attempt) as temporary:
        reader, receipt = admission.source_reader(source, Path(temporary))
        if receipt != parent["source"] or (source / "source.git/shallow").exists():
            raise SemanticError("DIAGNOSIS_SOURCE_DRIFT")
        for member in members:
            checkpoint()
            sha = member["commit_id"]
            row = original[sha]
            if coverage.transform(row, reader, encode) != current[sha]:
                raise SemanticError("DIAGNOSIS_CONTEXT_RECONSTRUCTION")
            reader.deadline = time.monotonic() + 30
            files = [
                (
                    admission.blob_text(reader, f["old_blob"], f["before_bytes"]),
                    admission.blob_text(reader, f["new_blob"], f["after_bytes"]),
                )
                for f in row["files"]
            ]
            groups, hunks = admission.hunk_groups(files)
            if hunks != current[sha]["context"]["hunks"]:
                raise SemanticError("DIAGNOSIS_HUNK_DRIFT")
            result = {
                "member": member,
                "roles": {
                    r: role_diagnosis(
                        groups[r],
                        current[sha]["context"]["baseline_roles"][r],
                        current[sha]["context"]["roles"][r],
                        encode,
                    )
                    for r in coverage.CODE_ROLES
                },
            }
            results.append(result)
            print(json.dumps({"diagnosed": len(results), "total": len(members)}), flush=True)
    with (attempt / "diagnosis.jsonl").open("xb") as stream:
        for result in results:
            stream.write(canonical(result) + b"\n")
    assets.write_json(
        attempt / "summary.json",
        {
            "selected": len(results),
            "residuals": 7,
            "missing_controls": missing,
            "exact_v3_reconstruction": True,
            "candidate_release": False,
            "full_population_certified": False,
            "new_classifier_fitted": False,
            "global_token_minimum_certified": False,
        },
    )
    if (
        artifacts.sources(SOURCES) != sources
        or artifacts.verify(inputs, checkpoint) != parent
        or artifacts.verify(declaration, checkpoint) != declared
        or artifacts.verify(old_dir, checkpoint) != old_manifest
        or artifacts.verify(checkpoint_dir, checkpoint) != pretrained
    ):
        raise SemanticError("DIAGNOSIS_EXECUTION_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "semantic-residual-diagnosis-v1",
            "accepted": len(results),
            "parent": parent["content_hash"],
            "declaration": declared["content_hash"],
            "sources": sources,
            "checkpoint": pretrained["content_hash"],
            "candidate_release": False,
        },
        ["diagnosis.jsonl", "summary.json"],
    )
