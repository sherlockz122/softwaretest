"""Immutable complete660 research population, with genuine bounded tail chunks."""

import json
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.mining.source_evidence import canonical, content_hash
from packages.semantic import adaptive, admission, artifacts, coverage, encoder, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

COUNT = 660
SLICES = tuple((i, min(i + 96, COUNT)) for i in range(0, COUNT, 96))
VERSION = "hunk-context-v5-population"
DECLARATION = "semantic-population-declaration-v1"
SOURCES = [
    *adaptive.SOURCES,
    "packages/semantic/population.py",
    "packages/semantic/population_encoder.py",
    "scripts/semantic_population_probe.py",
]


def probe_policy():
    return {
        "population": "all original660 v2, original file order; train488/validation172",
        "chunks": [list(s) for s in SLICES],
        "content_selection": adaptive.probe_policy(),
        "record_bytes_maximum": policy.MAX_RECORD,
        "controls": "all660 v2; available96 complete v3 and v4 only",
        "unsupported": "retain all; whole population encoding forbidden",
        "batch_invariance_certified": False,
    }


def slice_bounds(index):
    if type(index) is not int or not 0 <= index < len(SLICES):
        raise SemanticError("POPULATION_CHUNK_INDEX")
    return SLICES[index]


def validate_members(rows):
    if len(rows) != COUNT:
        raise SemanticError("POPULATION_COUNT")
    coverage.selection(rows)  # Canonical/SHA/unique identity/partitions/hunks, no labels.
    if any(not 0 < len(r["files"]) <= policy.MAX_FILES for r in rows):
        raise SemanticError("POPULATION_FILE_LIMIT")
    return [coverage.identity(r) for r in rows]


def load_bundle(v2, v3, v4, checkpoint_dir):
    dirs = [assets.safe_path(Path(p)) for p in (v2, v3, v4, checkpoint_dir)]
    parent, rows = coverage.parent_rows(dirs[0])
    members = validate_members(rows)
    old3, parent_dir, oldparent, _, control3 = adaptive.load_inputs(dirs[1])
    old4 = artifacts.verify(dirs[2], checkpoint)
    control4 = list(artifacts.records(dirs[2]))
    weights = coverage.safe_weights(dirs[3])
    if (
        parent_dir != dirs[0]
        or oldparent != parent
        or old3.get("checkpoint") != weights["content_hash"]
        or old4.get("version") != adaptive.VERSION
        or old4.get("accepted") != 96
        or len(control4) != 96
        or old4.get("sources") != artifacts.sources(adaptive.SOURCES)
        or old4.get("policy") != adaptive.probe_policy()
        or old4.get("parent") != parent["content_hash"]
        or old4.get("inputs") != old3["content_hash"]
        or old4.get("checkpoint") != weights["content_hash"]
        or old4.get("source", parent["source"]) != parent["source"]
        or old3.get("source") != parent["source"]
        or [coverage.identity(r) for r in control3] != [coverage.identity(r) for r in control4]
    ):
        raise SemanticError("POPULATION_CONTROL_BINDING")
    binding = {
        "directories": {
            k: p.relative_to(assets.ROOT).as_posix()
            for k, p in zip(("v2", "v3", "v4", "checkpoint"), dirs, strict=True)
        },
        "parents": [m["content_hash"] for m in (parent, old3, old4, weights)],
        "source": parent["source"],
        "members": content_hash(members),
        "sources": artifacts.sources(SOURCES),
        "policy": probe_policy(),
    }
    return (
        binding,
        rows,
        dict(
            zip(
                (r["member"]["commit_id"] for r in control3),
                zip(control3, control4, strict=True),
                strict=True,
            )
        ),
    )


def reload_bundle(binding):
    return load_bundle(
        *(assets.ROOT / binding["directories"][k] for k in ("v2", "v3", "v4", "checkpoint"))
    )


def assert_binding(binding):
    if reload_bundle(binding)[0] != binding:
        raise SemanticError("POPULATION_INPUT_DRIFT")


def declare(v2, v3, v4, checkpoint_dir, output):
    binding, rows, _ = load_bundle(v2, v3, v4, checkpoint_dir)
    output = assets.safe_path(Path(output))
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".declare-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(attempt / "selection.json", {"members": validate_members(rows)})
    assert_binding(binding)
    return artifacts.publish(
        attempt,
        output,
        {
            "version": DECLARATION,
            "accepted": COUNT,
            "binding": binding,
            "label_independent": True,
            "candidate_release": False,
        },
        ["selection.json"],
    )


def load_declaration(directory):
    directory = assets.safe_path(Path(directory))
    declared = artifacts.verify(directory, checkpoint)
    if declared.get("version") != DECLARATION or declared.get("accepted") != COUNT:
        raise SemanticError("POPULATION_DECLARATION")
    binding = declared["binding"]
    current, rows, controls = reload_bundle(binding)
    if current != binding or assets.read_json(directory / "selection.json", checkpoint) != {
        "members": validate_members(rows)
    }:
        raise SemanticError("POPULATION_DECLARATION_DRIFT")
    return declared, rows, controls


def transform(row, control, reader, tokenize, index):
    """Read each old/new blob once; recompute control functions from the same groups."""
    if not 0 < len(row["files"]) <= policy.MAX_FILES:
        raise SemanticError("POPULATION_FILE_LIMIT")
    reader.deadline = time.monotonic() + 30
    files = [
        (
            admission.blob_text(reader, f["old_blob"], f["before_bytes"]),
            admission.blob_text(reader, f["new_blob"], f["after_bytes"]),
        )
        for f in row["files"]
    ]
    groups, hunks = admission.hunk_groups(files)
    if hunks != row["context"]["hunks"] or not 0 < len(hunks) <= admission.MAX_HUNKS:
        raise SemanticError("POPULATION_HUNK_DRIFT")
    if content_hash(row["roles"]) != row["text_hash"]:
        raise SemanticError("POPULATION_V2_TEXT_DRIFT")
    old_roles, old_audits, baselines = {"message": row["roles"]["message"]}, {}, {}
    roles, audits = {"message": row["roles"]["message"]}, {}
    for role in coverage.CODE_ROLES:
        old_text, old_audit = admission.select_lines(groups[role], tokenize)
        if old_text != row["roles"][role] or any(
            row["context"]["roles"][role][k] != v for k, v in old_audit.items()
        ):
            raise SemanticError("POPULATION_V2_RECONSTRUCTION_DRIFT")
        if control is not None:
            old_roles[role], current = coverage.select_lines(
                groups[role], tokenize, baseline=old_audit["selected_coordinates"]
            )
            baselines[role] = coverage.line_coverage(groups[role], old_audit)
            old_audits[role] = coverage.line_coverage(groups[role], current) | {
                "v2_selected_lines_preserved": True
            }
        classified = adaptive.classify_groups(files, groups[role], hunks, role)
        roles[role], audits[role] = adaptive.select_lines(
            classified, old_audit["selected_coordinates"], tokenize
        )
    _, message = encoder.windows(tokenize(roles["message"]))
    context = {
        "version": adaptive.VERSION,
        "v2_reconstruction_exact": True,
        "v3_reconstruction_exact": control is not None,
        "v2_text_hash": row["text_hash"],
        "v3_text_hash": content_hash(old_roles) if control is not None else None,
        "roles": audits,
        "message": message,
        "supported": all(a["supported"] for a in audits.values()) and not message["omitted_tokens"],
        "full_commit_encoded": False,
    }
    result = {
        "member": coverage.identity(row),
        "roles": roles,
        "text_hash": content_hash(roles),
        "code_hash": content_hash({k: v for k, v in roles.items() if k != "message"}),
        "context": context,
    }
    if control is not None:
        previous = {
            "member": coverage.identity(row),
            "roles": old_roles,
            "text_hash": content_hash(old_roles),
            "code_hash": content_hash({k: v for k, v in old_roles.items() if k != "message"}),
            "context": {
                "version": "hunk-context-v3-bounded-probe",
                "hunks": hunks,
                "v2_text_hash": row["text_hash"],
                "v2_reconstruction_exact": True,
                "baseline_roles": baselines,
                "roles": old_audits,
                "full_commit_encoded": False,
            },
        }
        if previous != control[0] or result != control[1]:
            raise SemanticError("POPULATION_96_CONTROL_DRIFT")
    context["version"] = VERSION
    context["v4_control_exact"] = True if control is not None else None
    context["v3_reconstruction_exact"] = True if control is not None else None
    result["population_index"] = index
    if len(canonical(result)) > policy.MAX_RECORD:
        raise SemanticError("POPULATION_RECORD_LIMIT")
    return result


def validate_rows(rows, members, start=0, require_supported=False):
    if len(rows) != len(members) or not rows:
        raise SemanticError("POPULATION_ROWS_COUNT")
    for offset, (row, member) in enumerate(zip(rows, members, strict=True)):
        c = row["context"]
        if (
            row.get("population_index") != start + offset
            or row.get("member") != member
            or c.get("version") != VERSION
            or c.get("v2_reconstruction_exact") is not True
            or set(row["roles"]) != set(policy.ROLES)
            or content_hash(row["roles"]) != row["text_hash"]
            or content_hash({k: v for k, v in row["roles"].items() if k != "message"})
            != row["code_hash"]
        ):
            raise SemanticError("POPULATION_ROW_DRIFT")
        derived = not c["message"]["omitted_tokens"] and all(
            a["supported"] is True
            and not a["unsupported_reasons"]
            and a["v2_selected_lines_preserved"] is True
            and 0 <= a["selected_tokens"] <= adaptive.CONTENT_TOKENS
            for a in c["roles"].values()
        )
        if any(a.get("v2_selected_lines_preserved") is not True for a in c["roles"].values()):
            raise SemanticError("POPULATION_FLOOR_LOSS")
        if set(c["roles"]) != set(coverage.CODE_ROLES) or c.get("supported") is not derived:
            raise SemanticError("POPULATION_SUPPORT_DRIFT")
        if require_supported and not derived:
            raise SemanticError("POPULATION_UNSUPPORTED")


def validate_controls(rows, originals, controls):
    for row, old in zip(rows, originals, strict=True):
        c = row["context"]
        control = controls.get(old["member"]["commit_id"])
        if c.get("v2_text_hash") != old["text_hash"]:
            raise SemanticError("POPULATION_V2_PARENT_DRIFT")
        for role in coverage.CODE_ROLES:
            floor = {
                tuple(sorted(v.items()))
                for v in old["context"]["roles"][role]["selected_coordinates"]
            }
            current = [tuple(sorted(v.items())) for v in c["roles"][role]["selected_coordinates"]]
            if len(set(current)) != len(current) or not floor <= set(current):
                raise SemanticError("POPULATION_FLOOR_LOSS")
        if control is None:
            if any(
                c.get(k) is not None
                for k in ("v3_text_hash", "v3_reconstruction_exact", "v4_control_exact")
            ):
                raise SemanticError("POPULATION_FABRICATED_CONTROL")
        elif (
            any(c.get(k) is not True for k in ("v3_reconstruction_exact", "v4_control_exact"))
            or c.get("v3_text_hash") != control[0]["text_hash"]
            or any(row[k] != control[1][k] for k in ("member", "roles", "text_hash", "code_hash"))
            or any(c[k] != control[1]["context"][k] for k in ("roles", "message", "supported"))
        ):
            raise SemanticError("POPULATION_96_CONTROL_DRIFT")


def audit_rows(rows):
    return {
        "members": len(rows),
        "partitions": dict(Counter(r["member"]["partition"] for r in rows)),
        "supported": sum(r["context"]["supported"] for r in rows),
        "controls_exact": sum(r["context"]["v4_control_exact"] is True for r in rows),
        "unsupported_members": [
            {
                "population_index": r["population_index"],
                "member": r["member"],
                "roles": {
                    k: a["unsupported_reasons"]
                    for k, a in r["context"]["roles"].items()
                    if not a["supported"]
                },
                "message_omitted_tokens": r["context"]["message"]["omitted_tokens"],
            }
            for r in rows
            if not r["context"]["supported"]
        ],
        "full_change_certified": False,
        "semantic_quality_certified": False,
    }


def write_contexts(attempt, rows, licenses):
    with (attempt / "records.jsonl").open("xb") as stream:
        for row in rows:
            body = canonical(row)
            if len(body) > policy.MAX_RECORD:
                raise SemanticError("POPULATION_RECORD_LIMIT")
            stream.write(body + b"\n")
    (attempt / "licenses.json").write_bytes(licenses.read_bytes())
    assets.write_json(attempt / "audit.json", audit_rows(rows))


def contexts(declaration, source, output, index, tokenize=None):
    declaration, source, output = [assets.safe_path(Path(p)) for p in (declaration, source, output)]
    declared, originals, controls = load_declaration(declaration)
    binding = declared["binding"]
    start, stop = slice_bounds(index)
    if any(
        (source / "source.git" / p).exists()
        for p in (
            "shallow",
            "objects/info/alternates",
            "objects/info/http-alternates",
            "info/grafts",
        )
    ):
        raise SemanticError("POPULATION_UNSAFE_SOURCE")
    if tokenize is None:
        from transformers import RobertaTokenizer

        tokenizer = RobertaTokenizer.from_pretrained(
            assets.ROOT / binding["directories"]["checkpoint"], local_files_only=True
        )
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
    rows = []
    with TemporaryDirectory(dir=attempt) as temporary:
        reader, receipt = admission.source_reader(source, Path(temporary))
        if receipt != binding["source"]:
            raise SemanticError("POPULATION_SOURCE_DRIFT")
        for i in range(start, stop):
            checkpoint()
            old = originals[i]
            rows.append(
                transform(old, controls.get(old["member"]["commit_id"]), reader, tokenize, i)
            )
            if len(rows) % 8 == 0:
                print(
                    json.dumps(
                        {"chunk": index, "contexts_completed": len(rows), "total": stop - start}
                    ),
                    flush=True,
                )
        if assets.read_json(source / "acquisition.json", checkpoint) != receipt:
            raise SemanticError("POPULATION_SOURCE_DRIFT")
    validate_rows(rows, [coverage.identity(r) for r in originals[start:stop]], start)
    validate_controls(rows, originals[start:stop], controls)
    write_contexts(attempt, rows, assets.ROOT / binding["directories"]["v2"] / "licenses.json")
    if load_declaration(declaration)[0] != declared:
        raise SemanticError("POPULATION_DECLARATION_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION + "-chunk",
            "accepted": len(rows),
            "index": index,
            "range": [start, stop],
            "declaration": declared["content_hash"],
            "binding": binding,
            "supported": sum(r["context"]["supported"] for r in rows),
            "candidate_release": False,
        },
        ["records.jsonl", "licenses.json", "audit.json"],
    )


def chunk_manifests(parts, version, declaration_hash):
    if len(parts) != len(SLICES) or len({str(Path(p).resolve()) for p in parts}) != len(SLICES):
        raise SemanticError("POPULATION_CHUNK_SET")
    manifests = [artifacts.verify(Path(p), checkpoint) for p in parts]
    for i, m in enumerate(manifests):
        a, b = slice_bounds(i)
        if (
            m.get("version") != version
            or m.get("index") != i
            or m.get("range") != [a, b]
            or m.get("accepted") != b - a
            or m.get("declaration") != declaration_hash
        ):
            raise SemanticError("POPULATION_CHUNK_ORDER_OR_BINDING")
    return manifests


def merge_contexts(declaration, parts, output):
    declaration, output = [assets.safe_path(Path(p)) for p in (declaration, output)]
    parts = [assets.safe_path(Path(p)) for p in parts]
    declared, originals, controls = load_declaration(declaration)
    manifests = chunk_manifests(parts, VERSION + "-chunk", declared["content_hash"])
    rows = []
    for i, (part, manifest) in enumerate(zip(parts, manifests, strict=True)):
        a, b = slice_bounds(i)
        chunk = list(artifacts.records(part))
        validate_rows(chunk, [coverage.identity(r) for r in originals[a:b]], a)
        audit = audit_rows(chunk)
        if (
            manifest.get("binding") != declared["binding"]
            or manifest.get("supported") != audit["supported"]
            or assets.read_json(part / "audit.json", checkpoint) != audit
        ):
            raise SemanticError("POPULATION_CHUNK_CONTENT")
        rows.extend(chunk)
    validate_rows(rows, [coverage.identity(r) for r in originals])
    validate_controls(rows, originals, controls)
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".merge-" + uuid4().hex)
    attempt.mkdir()
    write_contexts(
        attempt, rows, assets.ROOT / declared["binding"]["directories"]["v2"] / "licenses.json"
    )
    if (
        load_declaration(declaration)[0] != declared
        or [artifacts.verify(p, checkpoint) for p in parts] != manifests
    ):
        raise SemanticError("POPULATION_MERGE_DRIFT")
    supported = sum(r["context"]["supported"] for r in rows)
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION,
            "accepted": COUNT,
            "supported": supported,
            "all_supported": supported == COUNT,
            "declaration": declared["content_hash"],
            "declaration_directory": declaration.relative_to(assets.ROOT).as_posix(),
            "binding": declared["binding"],
            "chunks": [m["content_hash"] for m in manifests],
            "candidate_release": False,
            "new_classifier_fitted": False,
            "old_model_compatible": False,
        },
        ["records.jsonl", "licenses.json", "audit.json"],
    )


def load_contexts(inputs, require_supported=False):
    inputs = assets.safe_path(Path(inputs))
    manifest = artifacts.verify(inputs, checkpoint)
    if manifest.get("version") != VERSION or manifest.get("accepted") != COUNT:
        raise SemanticError("POPULATION_CONTEXT_BINDING")
    declared, originals, controls = load_declaration(
        assets.ROOT / manifest["declaration_directory"]
    )
    if (
        manifest.get("declaration") != declared["content_hash"]
        or manifest.get("binding") != declared["binding"]
    ):
        raise SemanticError("POPULATION_CONTEXT_BINDING")
    rows = list(artifacts.records(inputs))
    validate_rows(
        rows, [coverage.identity(r) for r in originals], require_supported=require_supported
    )
    validate_controls(rows, originals, controls)
    audit = audit_rows(rows)
    if (
        assets.read_json(inputs / "audit.json", checkpoint) != audit
        or manifest.get("supported") != audit["supported"]
        or manifest.get("all_supported") is not (audit["supported"] == COUNT)
    ):
        raise SemanticError("POPULATION_CONTEXT_SUPPORT")
    return manifest, rows
