"""Frozen CPU role embeddings with explicit end-point window coverage and masks."""

import json
import math
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import numpy as np

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash
from packages.semantic import artifacts, policy
from packages.semantic.control import checkpoint
from packages.semantic.policy import SemanticError


def windows(ids, maximum=policy.TOKENS, count=policy.WINDOWS):
    if (
        type(maximum) is not int
        or not 4 <= maximum <= 512
        or type(count) is not int
        or not 1 <= count <= 8
    ):
        raise SemanticError("SEMANTIC_WINDOW_POLICY")
    payload = maximum - 2
    total = math.ceil(len(ids) / payload)
    selected = (
        list(range(total))
        if total <= count
        else np.linspace(0, total - 1, count, dtype=int).tolist()
    )
    starts = [i * payload for i in selected]
    chunks = [ids[i : i + payload] for i in starts]
    covered = sum(len(x) for x in chunks)
    return chunks, {
        "tokens": len(ids),
        "all_windows": total,
        "selected_starts": starts,
        "covered_tokens": covered,
        "omitted_tokens": len(ids) - covered,
        "empty": not ids,
    }


def infer(model, jobs, pad_id, device="cpu"):
    import torch

    if not jobs:
        return np.empty((0, model.config.hidden_size), dtype=np.float32)
    length = max(len(j) for j in jobs)
    ids = torch.full((len(jobs), length), pad_id, dtype=torch.long, device=device)
    attention = torch.zeros_like(ids)
    pooling = torch.zeros_like(ids, dtype=torch.bool)
    for index, job in enumerate(jobs):
        ids[index, : len(job)] = torch.tensor(job, device=device)
        attention[index, : len(job)] = 1
        pooling[index, 1 : len(job) - 1] = True
    with torch.inference_mode():
        hidden = model(input_ids=ids, attention_mask=attention).last_hidden_state
        weights = pooling.unsqueeze(-1)
        values = (hidden * weights).sum(dim=1) / weights.sum(dim=1).clamp(min=1)
    result = values.cpu().numpy().astype(np.float32, copy=False)
    if not np.isfinite(result).all():
        raise SemanticError("SEMANTIC_EMBEDDING_INVALID")
    return result


def encode(inputs, safe_checkpoint, output):
    import torch
    from transformers import RobertaModel, RobertaTokenizer

    inputs, safe_checkpoint, output = map(
        lambda p: assets.safe_path(Path(p)), (inputs, safe_checkpoint, output)
    )
    original = artifacts.verify(inputs, checkpoint)
    pretrained = artifacts.verify(safe_checkpoint, checkpoint)
    if pretrained.get("safe_format") != "safetensors" or pretrained.get("weights_only") is not True:
        raise SemanticError("SEMANTIC_UNSAFE_CHECKPOINT")
    if pretrained["original"]["revision"] != policy.REVISION:
        raise SemanticError("SEMANTIC_CHECKPOINT_CONFLICT")
    source_names = [
        "packages/semantic/encoder.py",
        "packages/semantic/policy.py",
        "packages/semantic/artifacts.py",
        "packages/semantic/control.py",
        "scripts/semantic_pilot.py",
        "tools/semantic/uv.lock",
    ]
    execution = {
        "device": "cpu",
        "dtype": "float32",
        "threads": policy.THREADS,
        "batch": policy.BATCH,
        "tokens": policy.TOKENS,
        "windows_per_role": policy.WINDOWS,
        "roles": policy.ROLES,
        "pooling": "content-token masked mean, token-weighted windows; role masks appended",
        "window_selection": "uniform endpoints, omitted coverage explicit",
    }
    dependencies = {
        p: version(p) for p in ["torch", "transformers", "tokenizers", "safetensors", "numpy"]
    }
    binding = {
        "inputs": original["content_hash"],
        "checkpoint": pretrained["content_hash"],
        "execution": execution,
        "sources": artifacts.sources(source_names),
        "dependencies": dependencies,
    }
    cache_key = content_hash(binding)
    output.mkdir(parents=True, exist_ok=True)
    directories = [p for p in output.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(directories) > 100:
        raise SemanticError("SEMANTIC_CACHE_INDEX_LIMIT")
    for directory in directories:
        manifest = artifacts.verify(directory, checkpoint)
        if manifest.get("cache_key") == cache_key:
            print(json.dumps({"verified_cache_reuse": True, "cache_key": cache_key}), flush=True)
            return directory
    rows = list(artifacts.records(inputs))
    if len(rows) != original["accepted"]:
        raise SemanticError("SEMANTIC_POPULATION_DRIFT")
    started = time.monotonic()
    torch.set_num_threads(policy.THREADS)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    tokenizer = RobertaTokenizer.from_pretrained(safe_checkpoint, local_files_only=True)
    tokenizer.model_max_length = 10**12
    if (tokenizer.bos_token_id, tokenizer.eos_token_id, tokenizer.pad_token_id) != (0, 2, 1):
        raise SemanticError("SEMANTIC_SPECIAL_TOKEN_CONFLICT")
    model = RobertaModel.from_pretrained(
        safe_checkpoint, local_files_only=True, use_safetensors=True, attn_implementation="eager"
    )
    model.requires_grad_(False)
    model.eval()
    if model.config.hidden_size != 768:
        raise SemanticError("SEMANTIC_DIMENSION_CONFLICT")
    sums = np.zeros((len(rows), 4, 768), dtype=np.float64)
    counts = np.zeros((len(rows), 4), dtype=np.int64)
    jobs, locations, coverage = [], [], []
    for index, row in enumerate(rows):
        checkpoint()
        roles = {}
        for role_index, role in enumerate(policy.ROLES):
            ids = tokenizer.encode(row["roles"][role], add_special_tokens=False)
            chunks, roles[role] = windows(ids)
            for chunk in chunks:
                jobs.append([tokenizer.bos_token_id, *chunk, tokenizer.eos_token_id])
                locations.append((index, role_index))
        coverage.append({"identity": row["member"]["commit_id"], "roles": roles})
    repeated = False
    for start in range(0, len(jobs), policy.BATCH):
        checkpoint()
        batch = jobs[start : start + policy.BATCH]
        value = infer(model, batch, tokenizer.pad_token_id)
        if start == 0:
            if not np.array_equal(value, infer(model, batch, tokenizer.pad_token_id)):
                raise SemanticError("SEMANTIC_NUMERIC_REPRODUCTION_DRIFT")
            repeated = True
        for local, ((index, role), vector) in enumerate(
            zip(locations[start : start + policy.BATCH], value, strict=True)
        ):
            # Each selected content token has equal weight, including a short tail window.
            weight = len(batch[local]) - 2
            sums[index, role] += vector * weight
            counts[index, role] += weight
        if start % (policy.BATCH * 25) == 0:
            print(
                json.dumps(
                    {
                        "encoded_windows": min(start + policy.BATCH, len(jobs)),
                        "total_windows": len(jobs),
                        "seconds": time.monotonic() - started,
                    }
                ),
                flush=True,
            )
    vectors = np.divide(
        sums, counts[:, :, None], out=np.zeros_like(sums), where=counts[:, :, None] > 0
    )
    values = np.concatenate(
        [vectors.reshape(len(rows), -1), (counts > 0).astype(float)], axis=1
    ).astype(np.float32)
    attempt = assets.safe_path(output / (".encode-" + uuid4().hex))
    attempt.mkdir()
    np.save(attempt / "embeddings.npy", values, allow_pickle=False)
    assets.write_json(attempt / "coverage.json", {"records": coverage})
    assets.write_json(
        attempt / "rows.json",
        {"records": [{"member": r["member"], "text_hash": r["text_hash"]} for r in rows]},
    )
    if (
        artifacts.verify(inputs, checkpoint) != original
        or artifacts.verify(safe_checkpoint, checkpoint) != pretrained
    ):
        raise SemanticError("SEMANTIC_INPUT_DRIFT")
    metadata = {
        "version": "frozen-codebert-role-cache-v1",
        "cache_key": cache_key,
        "binding": binding,
        "accepted": len(rows),
        "shape": list(values.shape),
        "input_directory": inputs.relative_to(assets.ROOT).as_posix(),
        "seconds": time.monotonic() - started,
        "windows": len(jobs),
        "first_batch_repeat_exact": repeated,
        "reproduction_scope": (
            "all input/file hashes and loaded cache exact; first batch forward repeated; "
            "no claim of full second extraction"
        ),
        "model_frozen": True,
        "all_parameters_frozen": all(not p.requires_grad for p in model.parameters()),
    }
    checkpoint()
    return artifacts.publish(
        attempt, output, metadata, ["embeddings.npy", "coverage.json", "rows.json"]
    )
