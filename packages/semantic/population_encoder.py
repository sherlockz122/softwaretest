"""Complete selected-token encoding in immutable <=96 slices, without padding."""

import os
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import numpy as np

from packages.mining import public_assets as assets
from packages.semantic import (
    adaptive,
    adaptive_encoder,
    artifacts,
    coverage_encoder,
    encoder,
    policy,
)
from packages.semantic import population as pop
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

VERSION = "frozen-codebert-population-v5"
FILES = ["embeddings.npy", "rows.json", "coverage.json"]


def protocol():
    return {
        "device": "cpu",
        "dtype": "float32",
        "threads": policy.THREADS,
        "batch": policy.BATCH,
        "tokens": policy.TOKENS,
        "code_windows_maximum": adaptive.CODE_WINDOWS,
        "message_windows": policy.WINDOWS,
        "pooling": "content-token masked mean, token-weighted windows; 4 role masks",
        "chunk_slices": [list(s) for s in pop.SLICES],
        "window_selection": "contiguous complete selected code tokens; adaptive count",
    }


def row_records(rows):
    return [
        {
            "population_index": r["population_index"],
            "member": r["member"],
            "text_hash": r["text_hash"],
        }
        for r in rows
    ]


def expected_audits(rows):
    return [
        {
            "population_index": r["population_index"],
            "member": r["member"],
            "roles": {
                k: r["context"]["message"]
                if k == "message"
                else adaptive_encoder.windows(range(r["context"]["roles"][k]["selected_tokens"]))[1]
                for k in policy.ROLES
            },
        }
        for r in rows
    ]


def prepare_jobs(rows, tokenize):
    if not 0 < len(rows) <= 96:
        raise SemanticError("POPULATION_ENCODING_SLICE")
    jobs, locations, audits = [], [], []
    for i, row in enumerate(rows):
        roles = {}
        for ri, role in enumerate(policy.ROLES):
            checkpoint()
            ids = tokenize(row["roles"][role])
            chunks, audit = (
                encoder.windows(ids) if role == "message" else adaptive_encoder.windows(ids)
            )
            expected = row["context"]["message"] if role == "message" else None
            if (
                audit["omitted_tokens"]
                or (expected is not None and audit != expected)
                or (
                    role != "message"
                    and len(ids) != row["context"]["roles"][role]["selected_tokens"]
                )
            ):
                raise SemanticError("POPULATION_TOKEN_DRIFT_OR_OMISSION")
            roles[role] = audit
            for chunk in chunks:
                jobs.append([0, *chunk, 2])
                locations.append((i, ri))
        audits.append(
            {"population_index": row["population_index"], "member": row["member"], "roles": roles}
        )
    if audits != expected_audits(rows):
        raise SemanticError("POPULATION_COVERAGE_DRIFT")
    return jobs, locations, audits


def encode(inputs, output, index):
    inputs, output = [assets.safe_path(Path(p)) for p in (inputs, output)]
    # The complete660 gate runs before importing/loading torch/tokenizer, for EVERY slice.
    original, all_rows = pop.load_contexts(inputs, require_supported=True)
    a, b = pop.slice_bounds(index)
    rows = all_rows[a:b]
    executed = artifacts.sources(pop.SOURCES)
    if original["binding"]["sources"] != executed:
        raise SemanticError("POPULATION_ENCODING_SOURCE_DRIFT")
    checkpoint_dir = assets.ROOT / original["binding"]["directories"]["checkpoint"]
    import torch
    from transformers import RobertaModel, RobertaTokenizer

    binding = {
        "inputs": original["content_hash"],
        "declaration": original["declaration"],
        "checkpoint": original["binding"]["parents"][3],
        "sources": executed,
        "dependencies": {
            p: version(p) for p in ("torch", "transformers", "tokenizers", "safetensors", "numpy")
        },
        "execution": protocol(),
    }
    torch.set_num_threads(policy.THREADS)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    started = time.monotonic()
    tokenizer = RobertaTokenizer.from_pretrained(checkpoint_dir, local_files_only=True)
    tokenizer.model_max_length = 10**12
    if (tokenizer.bos_token_id, tokenizer.eos_token_id, tokenizer.pad_token_id) != (0, 2, 1):
        raise SemanticError("POPULATION_SPECIAL_TOKENS")
    jobs, locations, audits = prepare_jobs(
        rows, lambda s: tokenizer.encode(s, add_special_tokens=False)
    )
    model = RobertaModel.from_pretrained(
        checkpoint_dir, local_files_only=True, use_safetensors=True, attn_implementation="eager"
    )
    model.requires_grad_(False)
    model.eval()
    if model.config.hidden_size != 768:
        raise SemanticError("POPULATION_DIMENSION")
    values, repeated = coverage_encoder.aggregate(
        jobs,
        locations,
        lambda batch: encoder.infer(model, batch, tokenizer.pad_token_id),
        len(rows),
    )
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".encode-" + uuid4().hex)
    attempt.mkdir()
    np.save(attempt / "embeddings.npy", values, allow_pickle=False)
    assets.write_json(attempt / "rows.json", {"records": row_records(rows)})
    assets.write_json(attempt / "coverage.json", {"records": audits})
    if pop.load_contexts(inputs, require_supported=True)[0] != original:
        raise SemanticError("POPULATION_ENCODING_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION + "-chunk",
            "accepted": len(rows),
            "index": index,
            "range": [a, b],
            "declaration": original["declaration"],
            "shape": list(values.shape),
            "binding": binding,
            "operation_id": uuid4().hex,
            "process_id": os.getpid(),
            "computed_new_forward": True,
            "windows": len(jobs),
            "first_batch_repeat_exact": repeated,
            "seconds": time.monotonic() - started,
            "model_frozen": all(not p.requires_grad for p in model.parameters()),
            "old_model_compatible": False,
            "new_classifier_fitted": False,
            "candidate_release": False,
        },
        FILES,
    )


def read_payload(directory, rows):
    values = np.load(directory / "embeddings.npy", allow_pickle=False)
    if (
        values.shape != (len(rows), 3076)
        or values.dtype != np.float32
        or not np.isfinite(values).all()
        or assets.read_json(directory / "rows.json", checkpoint, maximum=1024**3)
        != {"records": row_records(rows)}
        or assets.read_json(directory / "coverage.json", checkpoint, maximum=1024**3)
        != {"records": expected_audits(rows)}
    ):
        raise SemanticError("POPULATION_ENCODING_PAYLOAD")
    # Empty roles must have zero vectors; nonempty role masks must be1, never fake padding.
    masks = np.asarray(
        [[int(a["roles"][k]["tokens"] > 0) for k in policy.ROLES] for a in expected_audits(rows)],
        dtype=np.float32,
    )
    if not np.array_equal(values[:, -4:], masks):
        raise SemanticError("POPULATION_ENCODING_MASK")
    for ri in range(4):
        if np.any(values[masks[:, ri] == 0, ri * 768 : (ri + 1) * 768] != 0):
            raise SemanticError("POPULATION_ENCODING_EMPTY_ROLE")
    return values


def validate_forward(m):
    if (
        any(
            m.get(k) is not True
            for k in ("computed_new_forward", "model_frozen", "first_batch_repeat_exact")
        )
        or not isinstance(m.get("operation_id"), str)
        or len(m["operation_id"]) != 32
        or type(m.get("process_id")) is not int
        or m["process_id"] <= 0
    ):
        raise SemanticError("POPULATION_FORWARD_EVIDENCE")


def merge(inputs, parts, output):
    inputs, output = [assets.safe_path(Path(p)) for p in (inputs, output)]
    parts = [assets.safe_path(Path(p)) for p in parts]
    original, rows = pop.load_contexts(inputs, require_supported=True)
    manifests = pop.chunk_manifests(parts, VERSION + "-chunk", original["declaration"])
    binding = manifests[0]["binding"]
    if (
        binding.get("inputs") != original["content_hash"]
        or binding.get("checkpoint") != original["binding"]["parents"][3]
        or binding.get("sources") != artifacts.sources(pop.SOURCES)
        or binding.get("declaration") != original["declaration"]
        or binding.get("execution") != protocol()
        or set(binding.get("dependencies", {}))
        != {"torch", "transformers", "tokenizers", "safetensors", "numpy"}
        or any(not isinstance(v, str) or not v for v in binding["dependencies"].values())
        or len({m.get("operation_id") for m in manifests}) != 7
    ):
        raise SemanticError("POPULATION_ENCODING_BINDING")
    arrays, operations = [], []
    for i, (part, m) in enumerate(zip(parts, manifests, strict=True)):
        a, b = pop.slice_bounds(i)
        validate_forward(m)
        expected_windows = sum(
            x["roles"][k]["all_windows"] for x in expected_audits(rows[a:b]) for k in policy.ROLES
        )
        if (
            m.get("binding") != binding
            or m.get("shape") != [b - a, 3076]
            or m.get("windows") != expected_windows
        ):
            raise SemanticError("POPULATION_ENCODING_CHUNK_DRIFT")
        arrays.append(read_payload(part, rows[a:b]))
        operations.append(
            {
                k: m[k]
                for k in (
                    "operation_id",
                    "process_id",
                    "computed_new_forward",
                    "model_frozen",
                    "first_batch_repeat_exact",
                )
            }
        )
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".merge-" + uuid4().hex)
    attempt.mkdir()
    np.save(attempt / "embeddings.npy", np.concatenate(arrays), allow_pickle=False)
    assets.write_json(attempt / "rows.json", {"records": row_records(rows)})
    assets.write_json(attempt / "coverage.json", {"records": expected_audits(rows)})
    if (
        pop.load_contexts(inputs, require_supported=True)[0] != original
        or [artifacts.verify(p, checkpoint) for p in parts] != manifests
    ):
        raise SemanticError("POPULATION_ENCODING_MERGE_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION,
            "accepted": pop.COUNT,
            "shape": [pop.COUNT, 3076],
            "binding": binding,
            "chunks": [m["content_hash"] for m in manifests],
            "operations": operations,
            "input_directory": inputs.relative_to(assets.ROOT).as_posix(),
            "candidate_release": False,
            "new_classifier_fitted": False,
            "old_model_compatible": False,
        },
        FILES,
    )


def compare(first, second, output):
    first, second, output = [assets.safe_path(Path(p)) for p in (first, second, output)]
    left, right = [artifacts.verify(p, checkpoint) for p in (first, second)]
    if (
        first == second
        or any(
            m.get("version") != VERSION
            or m.get("accepted") != pop.COUNT
            or m.get("shape") != [pop.COUNT, 3076]
            for m in (left, right)
        )
        or left.get("binding") != right.get("binding")
        or left.get("binding", {}).get("sources") != artifacts.sources(pop.SOURCES)
        or left.get("binding", {}).get("execution") != protocol()
    ):
        raise SemanticError("POPULATION_REPEAT_BINDING")
    original, rows = pop.load_contexts(
        assets.ROOT / left["input_directory"], require_supported=True
    )
    if original["content_hash"] != left["binding"]["inputs"]:
        raise SemanticError("POPULATION_REPEAT_INPUT")
    for m in (left, right):
        if len(m.get("operations", [])) != 7:
            raise SemanticError("POPULATION_REPEAT_OPERATIONS")
        for operation in m["operations"]:
            validate_forward(operation)
    opids = [x["operation_id"] for m in (left, right) for x in m["operations"]]
    if len(set(opids)) != 14:
        raise SemanticError("POPULATION_REPEAT_OPERATIONS")
    a, b = (read_payload(p, rows) for p in (first, second))
    if left["files"] != right["files"] or not np.array_equal(a, b):
        raise SemanticError("POPULATION_REPEAT_VALUES")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".compare-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(
        attempt / "comparison.json",
        {
            "records": pop.COUNT,
            "all_three_payload_files_bytes_exact": True,
            "fourteen_distinct_forward_operations": True,
            "arbitrary_serving_batch_invariance_certified": False,
        },
    )
    if [artifacts.verify(p, checkpoint) for p in (first, second)] != [
        left,
        right,
    ] or pop.load_contexts(assets.ROOT / left["input_directory"], require_supported=True)[
        0
    ] != original:
        raise SemanticError("POPULATION_COMPARISON_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "population-encoding-comparison-v1",
            "accepted": pop.COUNT,
            "first": left["content_hash"],
            "second": right["content_hash"],
            "sources": artifacts.sources(pop.SOURCES),
            "candidate_release": False,
        },
        ["comparison.json"],
    )
