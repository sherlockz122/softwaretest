"""Content-priority whole-line research contexts; unsupported members remain explicit."""

import json
import math
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical, content_hash
from packages.semantic import admission, artifacts, coverage, encoder, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

CONTENT_TOKENS = 4032
CODE_WINDOWS = 32
VERSION = "hunk-context-v4-adaptive-probe"
SOURCES = [
    *coverage.SOURCES,
    "packages/semantic/adaptive.py",
    "packages/semantic/adaptive_encoder.py",
    "packages/semantic/coverage_encoder.py",
    "scripts/semantic_adaptive_probe.py",
    "packages/mining/public_assets.py",
    "packages/mining/source_evidence.py",
]


def probe_policy():
    return {
        "population": "same canonical v3 96, train64/validation32; prior order",
        "preserve_v2_selected_lines": True,
        "preserve_v3_extra_lines": False,
        "candidate_lines_per_hunk": 32,
        "required_class_scope": "complete eligible range before cap",
        "class_priority": ["base changed old line", "Unicode alphanumeric", "nonblank"],
        "representative": "least isolated tokens within required class; coordinate ties",
        "extra_budget_filling": False,
        "code_tokens": CONTENT_TOKENS,
        "code_windows": CODE_WINDOWS,
        "tokens_per_window": policy.TOKENS,
        "message_windows": policy.WINDOWS,
        "unsupported": "retain all members; forbid whole-population encoding",
        "lexical_proxy_is_semantic_certification": False,
        "full_change_certified": False,
    }


def load_inputs(directory):
    manifest = artifacts.verify(directory, checkpoint)
    if (
        manifest.get("version") != "hunk-context-v3-bounded-probe"
        or manifest.get("accepted") != 96
        or manifest.get("policy") != coverage.probe_policy()
        or manifest.get("sources") != artifacts.sources(coverage.SOURCES)
    ):
        raise SemanticError("ADAPTIVE_PARENT")
    parent_dir = assets.safe_path(assets.ROOT / manifest["parent_directory"])
    parent, rows = coverage.parent_rows(parent_dir)
    if manifest.get("parent") != parent["content_hash"]:
        raise SemanticError("ADAPTIVE_PARENT_DRIFT")
    chosen, _ = coverage.selection(rows)
    previous = list(artifacts.records(directory))
    if len(previous) != 96 or [coverage.identity(r) for r in previous] != [
        {k: v for k, v in m.items() if k not in {"hunks", "stratum"}} for m in chosen
    ]:
        raise SemanticError("ADAPTIVE_MEMBERSHIP_DRIFT")
    return manifest, parent_dir, parent, rows, previous


def declare(inputs, checkpoint_dir, output):
    inputs, checkpoint_dir, output = [
        assets.safe_path(Path(p)) for p in (inputs, checkpoint_dir, output)
    ]
    sources = artifacts.sources(SOURCES)
    manifest, parent_dir, parent, _, rows = load_inputs(inputs)
    pretrained = coverage.safe_weights(checkpoint_dir)
    if manifest.get("checkpoint") != pretrained["content_hash"]:
        raise SemanticError("ADAPTIVE_CHECKPOINT")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".declare-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(attempt / "selection.json", {"members": [coverage.identity(r) for r in rows]})
    if (
        artifacts.verify(inputs, checkpoint) != manifest
        or artifacts.verify(parent_dir, checkpoint) != parent
        or coverage.safe_weights(checkpoint_dir) != pretrained
        or artifacts.sources(SOURCES) != sources
    ):
        raise SemanticError("ADAPTIVE_DECLARATION_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "adaptive-context-declaration-v1",
            "accepted": 96,
            "inputs": manifest["content_hash"],
            "parent": parent["content_hash"],
            "checkpoint": pretrained["content_hash"],
            "input_directory": inputs.relative_to(assets.ROOT).as_posix(),
            "parent_directory": parent_dir.relative_to(assets.ROOT).as_posix(),
            "checkpoint_directory": checkpoint_dir.relative_to(assets.ROOT).as_posix(),
            "sources": sources,
            "policy": probe_policy(),
            "label_independent": True,
            "candidate_release": False,
        },
        ["selection.json"],
    )


def physical_lines(value):
    parts = value.split("\n")
    return [s + "\n" for s in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def content_class(role, line, body, hunk):
    old_start, old_stop = hunk["old"]
    return (
        int(role == "base" and old_start <= line < old_stop),
        int(any(c.isalnum() for c in body)),
        int(bool(body.strip())),
    )


def classify_groups(files, groups, hunks, role):
    """Inspect complete eligible ranges without silently extending the old candidate cap."""
    result = []
    for group in groups:
        checkpoint()
        hunk = hunks[group["hunk"]]
        fi = hunk["file"]
        lines = physical_lines(files[fi][1 if role == "added" else 0])
        if role == "base":
            start, stop = hunk["old"]
            indices = range(
                max(0, start - admission.RADIUS),
                min(len(lines), max(start + 1, stop) + admission.RADIUS),
            )
        else:
            start, stop = hunk["new" if role == "added" else "old"]
            indices = range(start, stop)
        ranks = {i: content_class(role, i, lines[i], hunk) for i in indices}
        if not ranks or len(ranks) != group["eligible_lines"]:
            raise SemanticError("ADAPTIVE_ELIGIBLE_DRIFT")
        required = max(ranks.values())
        candidates = group["candidates"]
        if not 0 < len(candidates) <= admission.LINES_PER_HUNK or any(
            f != fi or i not in ranks or lines[i] != body for f, i, body in candidates
        ):
            raise SemanticError("ADAPTIVE_CANDIDATE_DRIFT")
        result.append(
            group
            | {
                "required_class": list(required),
                "required_candidates": [(f, i) for f, i, _ in candidates if ranks[i] == required],
                "eligible_nonblank": sum(v[2] for v in ranks.values()),
                "eligible_alphanumeric": sum(v[1] for v in ranks.values()),
                "eligible_changed_old": sum(v[0] for v in ranks.values()),
                "candidate_classes": [list(ranks[i]) for _, i, _ in candidates],
            }
        )
    return result


def select_lines(groups, floor, tokenize):
    if len(groups) > admission.MAX_HUNKS:
        raise SemanticError("ADAPTIVE_HUNK_LIMIT")
    available = {}
    for g in groups:
        for f, line, body in g["candidates"]:
            if (f, line) in available and available[f, line] != body:
                raise SemanticError("ADAPTIVE_SHARED_BODY_DRIFT")
            available[f, line] = body
    selected = {}
    for coordinate in floor:
        key = coordinate["file"], coordinate["line"] - 1
        if key not in available or key in selected:
            raise SemanticError("ADAPTIVE_FLOOR_DRIFT")
        selected[key] = available[key]
    old_points = set(selected)
    reasons, costs = [], {}
    for group in sorted(groups, key=lambda g: g["hunk"]):
        checkpoint()
        required = [tuple(p) for p in group["required_candidates"]]
        if not required:
            reasons.append(
                {"code": "required_content_outside_candidate_cap", "hunk": group["hunk"]}
            )
            continue
        if set(required) & selected.keys():
            continue
        for key in required:
            if key not in costs:
                checkpoint()
                costs[key] = len(tokenize(available[key]))
        best = min(required, key=lambda k: (costs[k], *k))
        selected[best] = available[best]
    value = coverage.joined_lines(selected)
    tokens = len(tokenize(value))
    if tokens > CONTENT_TOKENS:
        reasons.append(
            {"code": "content_token_budget", "actual_tokens": tokens, "limit": CONTENT_TOKENS}
        )
    per_hunk = []
    for group in groups:
        candidates = group["candidates"]
        chosen = [
            rank
            for (f, i, _), rank in zip(candidates, group["candidate_classes"], strict=True)
            if (f, i) in selected
        ]
        per_hunk.append(
            {
                "hunk": group["hunk"],
                "required_class": group["required_class"],
                "required_class_selected": bool(
                    set(map(tuple, group["required_candidates"])) & selected.keys()
                ),
                "structurally_represented": bool(chosen),
                "eligible_lines": group["eligible_lines"],
                "eligible_nonblank": group["eligible_nonblank"],
                "eligible_alphanumeric": group["eligible_alphanumeric"],
                "eligible_changed_old": group["eligible_changed_old"],
                "candidate_lines": len(candidates),
                "selected_incidences": len(chosen),
                "selected_nonblank": sum(v[2] for v in chosen),
                "selected_alphanumeric": sum(v[1] for v in chosen),
                "selected_changed_old": sum(v[0] for v in chosen),
            }
        )
    return value, {
        "selected_coordinates": [{"file": f, "line": i + 1} for f, i in sorted(selected)],
        "v2_selected_lines_preserved": old_points <= selected.keys(),
        "selected_tokens": tokens,
        "required_windows": math.ceil(tokens / (policy.TOKENS - 2)),
        "supported": not reasons,
        "unsupported_reasons": reasons,
        "candidate_cap_omitted_incidences": sum(
            g["eligible_lines"] - len(g["candidates"]) for g in groups
        ),
        "candidate_hunks": len(groups),
        "represented_hunks": sum(g["structurally_represented"] for g in per_hunk),
        "required_class_satisfied_hunks": sum(g["required_class_selected"] for g in per_hunk),
        "hunks": per_hunk,
        "complete_change_certified": False,
    }


def transform(row, previous, reader, tokenize):
    if row["member"]["partition"] not in coverage.QUOTAS:
        raise SemanticError("ADAPTIVE_OLD_TEST_FORBIDDEN")
    if coverage.transform(row, reader, tokenize) != previous:
        raise SemanticError("ADAPTIVE_V3_RECONSTRUCTION_DRIFT")
    reader.deadline = time.monotonic() + 30
    files = [
        (
            admission.blob_text(reader, f["old_blob"], f["before_bytes"]),
            admission.blob_text(reader, f["new_blob"], f["after_bytes"]),
        )
        for f in row["files"]
    ]
    groups, hunks = admission.hunk_groups(files)
    if hunks != row["context"]["hunks"]:
        raise SemanticError("ADAPTIVE_HUNK_DRIFT")
    roles, audit = {"message": row["roles"]["message"]}, {}
    for role in coverage.CODE_ROLES:
        classified = classify_groups(files, groups[role], hunks, role)
        roles[role], audit[role] = select_lines(
            classified, row["context"]["roles"][role]["selected_coordinates"], tokenize
        )
    _, message = encoder.windows(tokenize(roles["message"]))
    return {
        "member": coverage.identity(row),
        "roles": roles,
        "text_hash": content_hash(roles),
        "code_hash": content_hash({k: v for k, v in roles.items() if k != "message"}),
        "context": {
            "version": VERSION,
            "v2_reconstruction_exact": True,
            "v3_reconstruction_exact": True,
            "v2_text_hash": row["text_hash"],
            "v3_text_hash": previous["text_hash"],
            "roles": audit,
            "message": message,
            "supported": all(a["supported"] for a in audit.values())
            and not message["omitted_tokens"],
            "full_commit_encoded": False,
        },
    }


def contexts(inputs, declaration, source, checkpoint_dir, output, tokenize=None):
    inputs, declaration, source, checkpoint_dir, output = [
        assets.safe_path(Path(p)) for p in (inputs, declaration, source, checkpoint_dir, output)
    ]
    executed = artifacts.sources(SOURCES)
    manifest, parent_dir, parent, rows, previous = load_inputs(inputs)
    declared = artifacts.verify(declaration, checkpoint)
    pretrained = coverage.safe_weights(checkpoint_dir)
    if (
        declared.get("version") != "adaptive-context-declaration-v1"
        or declared.get("accepted") != 96
        or declared.get("sources") != executed
        or declared.get("inputs") != manifest["content_hash"]
        or declared.get("parent") != parent["content_hash"]
        or declared.get("checkpoint") != pretrained["content_hash"]
        or declared.get("policy") != probe_policy()
        or assets.read_json(declaration / "selection.json", checkpoint)["members"]
        != [coverage.identity(r) for r in previous]
    ):
        raise SemanticError("ADAPTIVE_DECLARATION_DRIFT")
    if any(
        (source / "source.git" / p).exists()
        for p in (
            "shallow",
            "objects/info/alternates",
            "objects/info/http-alternates",
            "info/grafts",
        )
    ):
        raise SemanticError("ADAPTIVE_UNSAFE_SOURCE")
    if tokenize is None:
        from transformers import RobertaTokenizer

        tokenizer = RobertaTokenizer.from_pretrained(checkpoint_dir, local_files_only=True)
        tokenizer.model_max_length = 10**12

        @lru_cache(maxsize=4096)
        def cached(value):
            return tuple(tokenizer.encode(value, add_special_tokens=False))

        def tokenize(value):
            checkpoint()
            return (
                cached(value)
                if len(value) <= 4096
                else tokenizer.encode(value, add_special_tokens=False)
            )

    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".contexts-" + uuid4().hex)
    attempt.mkdir()
    lookup = {r["member"]["commit_id"]: r for r in rows}
    transformed = []
    with TemporaryDirectory(dir=attempt) as temporary:
        reader, receipt = admission.source_reader(source, Path(temporary))
        if receipt != manifest["source"] or receipt != parent["source"]:
            raise SemanticError("ADAPTIVE_SOURCE_DRIFT")
        for old in previous:
            checkpoint()
            transformed.append(transform(lookup[old["member"]["commit_id"]], old, reader, tokenize))
            if len(transformed) % 8 == 0:
                print(json.dumps({"contexts_completed": len(transformed), "total": 96}), flush=True)
    with (attempt / "records.jsonl").open("xb") as stream:
        for row in transformed:
            stream.write(canonical(row) + b"\n")
    (attempt / "licenses.json").write_bytes((inputs / "licenses.json").read_bytes())
    supported = sum(r["context"]["supported"] for r in transformed)
    assets.write_json(
        attempt / "audit.json",
        {
            "members": len(transformed),
            "supported": supported,
            "unsupported": len(transformed) - supported,
            "partitions": dict(Counter(r["member"]["partition"] for r in transformed)),
            "unsupported_members": [
                {
                    "member": r["member"],
                    "roles": {
                        role: a["unsupported_reasons"]
                        for role, a in r["context"]["roles"].items()
                        if not a["supported"]
                    },
                    "message_omitted_tokens": r["context"]["message"]["omitted_tokens"],
                }
                for r in transformed
                if not r["context"]["supported"]
            ],
            "all_v2_lines_preserved": all(
                a["v2_selected_lines_preserved"]
                for r in transformed
                for a in r["context"]["roles"].values()
            ),
            "lexical_proxy_is_semantic_certification": False,
            "full_population_660_certified": False,
            "new_classifier_fitted": False,
            "old_test_transformed": False,
        },
    )
    if (
        artifacts.verify(inputs, checkpoint) != manifest
        or artifacts.verify(parent_dir, checkpoint) != parent
        or artifacts.verify(declaration, checkpoint) != declared
        or coverage.safe_weights(checkpoint_dir) != pretrained
        or artifacts.sources(SOURCES) != executed
    ):
        raise SemanticError("ADAPTIVE_CONTEXT_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION,
            "accepted": 96,
            "supported": supported,
            "all_supported": supported == 96,
            "inputs": manifest["content_hash"],
            "parent": parent["content_hash"],
            "declaration": declared["content_hash"],
            "checkpoint": pretrained["content_hash"],
            "sources": executed,
            "policy": probe_policy(),
            "candidate_release": False,
            "new_classifier_fitted": False,
            "old_model_compatible": False,
        },
        ["records.jsonl", "audit.json", "licenses.json"],
    )
