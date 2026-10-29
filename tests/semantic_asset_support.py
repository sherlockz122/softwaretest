"""Synthetic structural fixtures, never model utility or source qualification evidence."""

import copy
import hashlib
import json
import struct
from pathlib import Path

from packages.mining import public_assets as assets
from packages.mining.source_evidence import content_hash


def write(path, value):
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def record(path):
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def source(root):
    directory = Path(root) / "source"
    directory.mkdir()
    rows, coverage = [], []
    for i in range(660):
        member = {
            "canonical_repository": "test/fixture",
            "commit_id": f"{i:040x}",
            "partition": "train" if i < 488 else "validation",
        }
        rows.append(
            {
                "population_index": i,
                "member": member,
                "text_hash": hashlib.sha256(str(i).encode()).hexdigest(),
            }
        )
        roles = {
            k: {
                "tokens": 1,
                "all_windows": 1,
                "covered_tokens": 1,
                "omitted_tokens": 0,
                "empty": False,
                "selected_starts": [0],
            }
            for k in ("message", "added", "deleted", "base")
        }
        coverage.append({"population_index": i, "member": copy.deepcopy(member), "roles": roles})
    write(directory / "rows.json", {"records": rows})
    write(directory / "coverage.json", {"records": coverage})
    header = b"{'descr': '<f4', 'fortran_order': False, 'shape': (660, 3076), }"
    header += b" " * ((-10 - len(header) - 1) % 64) + b"\n"
    one = struct.pack("<3076f", *([0.25] * 3072 + [1.0] * 4))
    with (directory / "embeddings.npy").open("wb") as stream:
        stream.write(b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header)
        for _ in range(660):
            stream.write(one)
    m = {
        "version": "frozen-codebert-population-v5",
        "accepted": 660,
        "shape": [660, 3076],
        "candidate_release": False,
        "new_classifier_fitted": False,
        "old_model_compatible": False,
        "binding": {
            "inputs": "a" * 64,
            "declaration": "b" * 64,
            "checkpoint": "c" * 64,
            "sources": {f"packages/fixture{i}.py": f"{i:064x}" for i in range(18)},
            "dependencies": {
                k: "test-fixture"
                for k in ("torch", "numpy", "transformers", "tokenizers", "safetensors")
            },
            "execution": {
                "device": "cpu",
                "dtype": "float32",
                "threads": 4,
                "batch": 8,
                "tokens": 128,
                "code_windows_maximum": 32,
                "message_windows": 2,
                "pooling": "content-token masked mean, token-weighted windows; 4 role masks",
                "chunk_slices": [[i, min(i + 96, 660)] for i in range(0, 660, 96)],
                "window_selection": "contiguous complete selected code tokens; adaptive count",
            },
        },
        "chunks": [f"{i:064x}" for i in range(7)],
        "operations": [
            {
                "operation_id": f"{i:032x}",
                "process_id": 1,
                "computed_new_forward": True,
                "first_batch_repeat_exact": True,
                "model_frozen": True,
            }
            for i in range(7)
        ],
        "input_directory": "runtime/synthetic-fixture-not-real-data",
        "files": {
            name: record(directory / name)
            for name in ("rows.json", "coverage.json", "embeddings.npy")
        },
    }
    return reseal(directory, m)


def reseal(directory, manifest, *, refresh_files=False):
    m = copy.deepcopy(manifest)
    m.pop("content_hash", None)
    if refresh_files:
        m["files"] = {k: record(directory / k) for k in m["files"]}
    m = assets.sealed(m)
    write(directory / "manifest.json", m)
    target = directory.with_name(content_hash(m))
    if target != directory:
        directory.rename(target)
    return target, m
