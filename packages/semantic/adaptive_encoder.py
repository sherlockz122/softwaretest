"""New complete contiguous32-window contract, independent of sealed old limits."""

import math
import os
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import numpy as np

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import adaptive, artifacts, coverage, coverage_encoder, encoder, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError

VERSION = "frozen-codebert-adaptive-window-probe-v4"


def windows(ids):
    if len(ids) > adaptive.CONTENT_TOKENS:
        raise SemanticError("ADAPTIVE_TOKEN_BUDGET")
    payload = policy.TOKENS - 2
    starts = list(range(0, len(ids), payload))
    chunks = [ids[i : i + payload] for i in starts]
    return chunks, {
        "tokens": len(ids),
        "all_windows": math.ceil(len(ids) / payload),
        "selected_starts": starts,
        "covered_tokens": sum(map(len, chunks)),
        "omitted_tokens": 0,
        "empty": not ids,
    }


def validate_rows(rows):
    if len(rows) != 96 or any(r["member"]["partition"] not in coverage.QUOTAS for r in rows):
        raise SemanticError("ADAPTIVE_ENCODING_POPULATION")
    if any(
        r["member"]["canonical_repository"] != policy.REPOSITORY
        or not isinstance(r["member"]["commit_id"], str)
        or len(r["member"]["commit_id"]) != 40
        or any(c not in "0123456789abcdef" for c in r["member"]["commit_id"])
        or r["member"]["commit_id"] == "0" * 40
        for r in rows
    ):
        raise SemanticError("ADAPTIVE_ENCODING_IDENTITY")
    if len({(r["member"]["canonical_repository"], r["member"]["commit_id"]) for r in rows}) != 96:
        raise SemanticError("ADAPTIVE_ENCODING_POPULATION")
    if sum(r["member"]["partition"] == "train" for r in rows) != 64:
        raise SemanticError("ADAPTIVE_ENCODING_POPULATION")
    if any(
        r["context"].get("version") != adaptive.VERSION
        or r["context"].get("supported") is not True
        or r["context"]["message"]["omitted_tokens"]
        or any(
            a["supported"] is not True or a["unsupported_reasons"]
            for a in r["context"]["roles"].values()
        )
        for r in rows
    ):
        raise SemanticError("ADAPTIVE_UNSUPPORTED_POPULATION")


def prepare_jobs(rows, tokenize):
    validate_rows(rows)
    jobs, locations, audits = [], [], []
    for index, row in enumerate(rows):
        if content_hash(row["roles"]) != row["text_hash"]:
            raise SemanticError("ADAPTIVE_TEXT_DRIFT")
        roles = {}
        for ri, role in enumerate(policy.ROLES):
            checkpoint()
            ids = tokenize(row["roles"][role])
            if role == "message":
                chunks, audit = encoder.windows(ids)
            else:
                chunks, audit = windows(ids)
                if len(ids) != row["context"]["roles"][role]["selected_tokens"]:
                    raise SemanticError("ADAPTIVE_TOKEN_DRIFT")
            if audit["omitted_tokens"]:
                raise SemanticError("ADAPTIVE_SELECTED_TOKENS_OMITTED")
            roles[role] = audit
            for chunk in chunks:
                jobs.append([0, *chunk, 2])
                locations.append((index, ri))
        audits.append({"member": row["member"], "roles": roles})
    return jobs, locations, audits


def encode(inputs, checkpoint_dir, output):
    inputs, checkpoint_dir, output = [
        assets.safe_path(Path(p)) for p in (inputs, checkpoint_dir, output)
    ]
    original = artifacts.verify(inputs, checkpoint)
    executed = artifacts.sources(adaptive.SOURCES)
    if (
        original.get("version") != adaptive.VERSION
        or original.get("accepted") != 96
        or original.get("all_supported") is not True
        or original.get("supported") != 96
        or original.get("policy") != adaptive.probe_policy()
        or original.get("sources") != executed
    ):
        raise SemanticError("ADAPTIVE_ENCODING_BINDING")
    rows = list(artifacts.records(inputs))
    validate_rows(rows)  # Reject unsupported before importing/loading torch or tokenizer.
    pretrained = coverage.safe_weights(checkpoint_dir)
    if original.get("checkpoint") != pretrained["content_hash"]:
        raise SemanticError("ADAPTIVE_CHECKPOINT")
    import torch
    from transformers import RobertaModel, RobertaTokenizer

    binding = {
        "inputs": original["content_hash"],
        "checkpoint": pretrained["content_hash"],
        "sources": executed,
        "dependencies": {
            p: version(p) for p in ("torch", "transformers", "tokenizers", "safetensors", "numpy")
        },
        "execution": {
            "device": "cpu",
            "dtype": "float32",
            "threads": policy.THREADS,
            "batch": policy.BATCH,
            "tokens": policy.TOKENS,
            "code_windows_maximum": adaptive.CODE_WINDOWS,
            "message_windows": policy.WINDOWS,
            "pooling": "content-token masked mean, token-weighted windows; 4 role masks",
            "window_selection": "contiguous complete selected code tokens; adaptive count",
        },
    }
    torch.set_num_threads(policy.THREADS)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    started = time.monotonic()
    tokenizer = RobertaTokenizer.from_pretrained(checkpoint_dir, local_files_only=True)
    tokenizer.model_max_length = 10**12
    if (tokenizer.bos_token_id, tokenizer.eos_token_id, tokenizer.pad_token_id) != (0, 2, 1):
        raise SemanticError("ADAPTIVE_SPECIAL_TOKENS")
    jobs, locations, audits = prepare_jobs(
        rows, lambda s: tokenizer.encode(s, add_special_tokens=False)
    )
    model = RobertaModel.from_pretrained(
        checkpoint_dir, local_files_only=True, use_safetensors=True, attn_implementation="eager"
    )
    model.requires_grad_(False)
    model.eval()
    if model.config.hidden_size != 768:
        raise SemanticError("ADAPTIVE_DIMENSION")
    values, repeated = coverage_encoder.aggregate(
        jobs, locations, lambda batch: encoder.infer(model, batch, tokenizer.pad_token_id), 96
    )
    if (
        artifacts.verify(inputs, checkpoint) != original
        or coverage.safe_weights(checkpoint_dir) != pretrained
        or artifacts.sources(adaptive.SOURCES) != executed
    ):
        raise SemanticError("ADAPTIVE_ENCODING_DRIFT")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".encode-" + uuid4().hex)
    attempt.mkdir()
    np.save(attempt / "embeddings.npy", values, allow_pickle=False)
    assets.write_json(attempt / "coverage.json", {"records": audits})
    assets.write_json(
        attempt / "rows.json",
        {"records": [{"member": r["member"], "text_hash": r["text_hash"]} for r in rows]},
    )
    if (
        artifacts.verify(inputs, checkpoint) != original
        or coverage.safe_weights(checkpoint_dir) != pretrained
        or artifacts.sources(adaptive.SOURCES) != executed
    ):
        raise SemanticError("ADAPTIVE_ENCODING_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": VERSION,
            "accepted": 96,
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
            "candidate_release": False,
            "new_classifier_fitted": False,
        },
        ["embeddings.npy", "coverage.json", "rows.json"],
    )


def compare(first, second, output):
    first, second, output = [assets.safe_path(Path(p)) for p in (first, second, output)]
    left, right = artifacts.verify(first, checkpoint), artifacts.verify(second, checkpoint)
    executed = artifacts.sources(adaptive.SOURCES)
    if (
        first == second
        or left.get("version") != VERSION
        or right.get("version") != VERSION
        or left.get("operation_id") == right.get("operation_id")
        or left.get("process_id") == right.get("process_id")
        or left.get("binding") != right.get("binding")
        or left.get("shape") != [96, 3076]
        or right.get("shape") != [96, 3076]
        or left.get("binding", {}).get("sources") != executed
        or any(
            m.get("computed_new_forward") is not True
            or m.get("model_frozen") is not True
            or m.get("first_batch_repeat_exact") is not True
            for m in (left, right)
        )
    ):
        raise SemanticError("ADAPTIVE_REPEAT_BINDING")
    a, b = (np.load(p / "embeddings.npy", allow_pickle=False) for p in (first, second))
    if (
        a.dtype != np.float32
        or b.dtype != np.float32
        or a.shape != (96, 3076)
        or b.shape != (96, 3076)
        or not np.isfinite(a).all()
        or not np.array_equal(a, b)
        or left["files"] != right["files"]
    ):
        raise SemanticError("ADAPTIVE_REPEAT_VALUES")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".compare-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(
        attempt / "comparison.json",
        {
            "all_files_bytes_exact": True,
            "values_exact": True,
            "records": 96,
            "independent_processes": True,
            "new_full_forward_both": True,
            "arbitrary_serving_batch_invariance_certified": False,
        },
    )
    if (
        artifacts.verify(first, checkpoint) != left
        or artifacts.verify(second, checkpoint) != right
        or artifacts.sources(adaptive.SOURCES) != executed
    ):
        raise SemanticError("ADAPTIVE_COMPARISON_DRIFT")
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "adaptive-encoding-comparison-v1",
            "accepted": 96,
            "first": left["content_hash"],
            "second": right["content_hash"],
            "sources": executed,
            "candidate_release": False,
        },
        ["comparison.json"],
    )
