"""Frozen bounded whole-line window probe, separately versioned from old caches."""

import json
import os
import time
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import numpy as np

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import artifacts, coverage, encoder, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError


def prepare_jobs(rows, tokenize, bos=0, eos=2):
    jobs, locations, audits = [], [], []
    for index, row in enumerate(rows):
        if row["member"]["partition"] not in coverage.QUOTAS:
            raise SemanticError("COVERAGE_OLD_TEST_FORBIDDEN")
        if content_hash(row["roles"]) != row["text_hash"]:
            raise SemanticError("COVERAGE_TEXT_DRIFT")
        roles = {}
        for role_index, role in enumerate(policy.ROLES):
            checkpoint()
            ids = tokenize(row["roles"][role])
            if role != "message" and len(ids) > coverage.CONTENT_TOKENS:
                raise SemanticError("COVERAGE_TOKEN_BUDGET")
            chunks, roles[role] = encoder.windows(
                ids, count=policy.WINDOWS if role == "message" else coverage.CODE_WINDOWS
            )
            if role != "message" and roles[role]["omitted_tokens"]:
                raise SemanticError("COVERAGE_SELECTED_CODE_TRUNCATED")
            for chunk in chunks:
                jobs.append([bos, *chunk, eos])
                locations.append((index, role_index))
        audits.append({"member": row["member"], "roles": roles})
    return jobs, locations, audits


def aggregate(jobs, locations, infer, records, *, hidden=768):
    if (
        len(jobs) != len(locations)
        or type(records) is not int
        or not 0 < records <= 96
        or type(hidden) is not int
        or not 0 < hidden <= 768
    ):
        raise SemanticError("COVERAGE_AGGREGATION_SHAPE")
    sums = np.zeros((records, 4, hidden), dtype=np.float64)
    counts = np.zeros((records, 4), dtype=np.int64)
    repeated = False
    for start in range(0, len(jobs), policy.BATCH):
        checkpoint()
        batch = jobs[start : start + policy.BATCH]
        values = infer(batch)
        if values.shape != (len(batch), hidden) or not np.isfinite(values).all():
            raise SemanticError("COVERAGE_FORWARD_INVALID")
        if start == 0:
            if not np.array_equal(values, infer(batch)):
                raise SemanticError("COVERAGE_FORWARD_REPEAT_DRIFT")
            repeated = True
        for local, ((row, role), vector) in enumerate(
            zip(locations[start : start + policy.BATCH], values, strict=True)
        ):
            if (
                not 0 <= row < records
                or not 0 <= role < 4
                or not 2 < len(batch[local]) <= policy.TOKENS
            ):
                raise SemanticError("COVERAGE_AGGREGATION_LOCATION")
            weight = len(batch[local]) - 2
            sums[row, role] += vector * weight
            counts[row, role] += weight
        if start % (policy.BATCH * 25) == 0:
            print(
                json.dumps(
                    {
                        "encoded_windows": min(start + policy.BATCH, len(jobs)),
                        "total_windows": len(jobs),
                    }
                ),
                flush=True,
            )
    means = np.divide(
        sums, counts[:, :, None], out=np.zeros_like(sums), where=counts[:, :, None] > 0
    )
    result = np.concatenate(
        [means.reshape(records, -1), (counts > 0).astype(float)], axis=1
    ).astype(np.float32)
    if not np.isfinite(result).all():
        raise SemanticError("COVERAGE_EMBEDDING_INVALID")
    return result, repeated


def encode(inputs, checkpoint_dir, output):
    import torch
    from transformers import RobertaModel, RobertaTokenizer

    inputs, checkpoint_dir, output = [
        assets.safe_path(Path(p)) for p in (inputs, checkpoint_dir, output)
    ]
    original = artifacts.verify(inputs, checkpoint)
    pretrained = coverage.safe_weights(checkpoint_dir)
    if (
        original.get("version") != "hunk-context-v3-bounded-probe"
        or original.get("accepted") != 96
        or original.get("checkpoint") != pretrained["content_hash"]
        or original.get("policy") != coverage.probe_policy()
        or original.get("sources") != artifacts.sources(coverage.SOURCES)
    ):
        raise SemanticError("COVERAGE_ENCODING_BINDING")
    rows = list(artifacts.records(inputs))
    if len(rows) != 96 or dict(Counter(r["member"]["partition"] for r in rows)) != {
        "train": 64,
        "validation": 32,
    }:
        raise SemanticError("COVERAGE_ENCODING_POPULATION")
    execution = {
        "device": "cpu",
        "dtype": "float32",
        "threads": policy.THREADS,
        "batch": policy.BATCH,
        "tokens": policy.TOKENS,
        "code_windows": coverage.CODE_WINDOWS,
        "message_windows": policy.WINDOWS,
        "pooling": "content-token masked mean, token-weighted windows; 4 role masks",
    }
    binding = {
        "inputs": original["content_hash"],
        "checkpoint": pretrained["content_hash"],
        "execution": execution,
        "sources": artifacts.sources(coverage.SOURCES),
        "dependencies": {
            p: version(p) for p in ("torch", "transformers", "tokenizers", "safetensors", "numpy")
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
        raise SemanticError("COVERAGE_SPECIAL_TOKENS")
    model = RobertaModel.from_pretrained(
        checkpoint_dir, local_files_only=True, use_safetensors=True, attn_implementation="eager"
    )
    model.requires_grad_(False)
    model.eval()
    if model.config.hidden_size != 768:
        raise SemanticError("COVERAGE_DIMENSION")
    jobs, locations, audits = prepare_jobs(
        rows, lambda s: tokenizer.encode(s, add_special_tokens=False)
    )
    values, repeated = aggregate(
        jobs,
        locations,
        lambda batch: encoder.infer(model, batch, tokenizer.pad_token_id),
        len(rows),
    )
    if (
        artifacts.verify(inputs, checkpoint) != original
        or artifacts.verify(checkpoint_dir, checkpoint) != pretrained
        or binding["sources"] != artifacts.sources(coverage.SOURCES)
    ):
        raise SemanticError("COVERAGE_ENCODING_DRIFT")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".encode-" + uuid4().hex)
    attempt.mkdir()
    np.save(attempt / "embeddings.npy", values, allow_pickle=False)
    assets.write_json(attempt / "coverage.json", {"records": audits})
    assets.write_json(
        attempt / "rows.json",
        {"records": [{"member": r["member"], "text_hash": r["text_hash"]} for r in rows]},
    )
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "frozen-codebert-hunk-window-probe-v3",
            "accepted": len(rows),
            "shape": list(values.shape),
            "binding": binding,
            "cache_key": content_hash(binding),
            "input_directory": inputs.relative_to(assets.ROOT).as_posix(),
            "checkpoint_directory": checkpoint_dir.relative_to(assets.ROOT).as_posix(),
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
    if (
        first == second
        or left.get("version") != "frozen-codebert-hunk-window-probe-v3"
        or right.get("version") != left["version"]
        or left.get("operation_id") == right.get("operation_id")
        or left.get("binding") != right.get("binding")
        or left.get("shape") != [96, 3076]
        or right.get("shape") != left["shape"]
        or not all(
            m.get("computed_new_forward") is True and m.get("model_frozen") is True
            for m in (left, right)
        )
        or left.get("binding", {}).get("sources") != artifacts.sources(coverage.SOURCES)
    ):
        raise SemanticError("COVERAGE_REPEAT_BINDING")
    a, b = (np.load(p / "embeddings.npy", allow_pickle=False) for p in (first, second))
    if (
        a.dtype != np.float32
        or b.dtype != a.dtype
        or a.shape != (96, 3076)
        or b.shape != a.shape
        or not np.isfinite(a).all()
        or not np.array_equal(a, b)
        or left["files"] != right["files"]
    ):
        raise SemanticError("COVERAGE_REPEAT_VALUES")
    output.mkdir(parents=True, exist_ok=True)
    attempt = output / (".comparison-" + uuid4().hex)
    attempt.mkdir()
    assets.write_json(
        attempt / "comparison.json",
        {
            "all_vectors_exact": True,
            "npy_bytes_exact": True,
            "rows_and_coverage_exact": True,
            "first": left["content_hash"],
            "second": right["content_hash"],
            "metadata_bytes_claimed_exact": False,
            "candidate_release": False,
            "nfr05_all_product_artifacts_certified": False,
        },
    )
    return artifacts.publish(
        attempt,
        output,
        {
            "version": "semantic-coverage-repeat-v1",
            "accepted": 96,
            "first": left["content_hash"],
            "second": right["content_hash"],
            "candidate_release": False,
        },
        ["comparison.json"],
    )
