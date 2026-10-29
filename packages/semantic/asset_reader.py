"""Versioned, allocation-bounded v5 payload reading; no model runtime imports."""

import ast
import hashlib
import json
import math
import os
import re
import stat
import struct
import time
from collections import Counter
from pathlib import Path

from packages.mining import public_assets as assets

PRODUCER = "frozen-codebert-population-v5"
COUNT, WIDTH = 660, 3076
ROLES = ("message", "added", "deleted", "base")
CAPS = {"manifest.json": 65536, "rows.json": 524288, "coverage.json": 1048576}
CAPS["embeddings.npy"] = COUNT * WIDTH * 4 + 4108
PAYLOADS = {"embeddings.npy", "rows.json", "coverage.json"}
HASH = re.compile(r"[0-9a-f]{64}\Z")


class ConsumptionError(ValueError):
    def __init__(self, code="SEMANTIC_ASSET_INVALID", status=409):
        self.code, self.status = code, status
        super().__init__(code)


def require(condition, code="SEMANTIC_ASSET_INVALID"):
    if not condition:
        raise ConsumptionError(code)


def digest_id(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


class Budget:
    def __init__(self, tick=lambda: None, seconds=60):
        require(type(seconds) in (float, int) and 0 < seconds <= 60)
        self.deadline = time.monotonic() + seconds
        self.tick = tick

    def check(self):
        self.tick()
        require(time.monotonic() <= self.deadline, "SEMANTIC_ASSET_DEADLINE")


def safe(path):
    try:
        return assets.safe_path(Path(path))
    except (assets.AssetError, OSError, ValueError) as exc:
        raise ConsumptionError("SEMANTIC_ASSET_PATH") from exc


def stamp(path):
    value = safe(path).stat()
    require(stat.S_ISREG(value.st_mode), "SEMANTIC_ASSET_FILE")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def opened_stamp(stream):
    value = os.fstat(stream.fileno())
    require(stat.S_ISREG(value.st_mode), "SEMANTIC_ASSET_FILE")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def descriptor(record, maximum):
    require(isinstance(record, dict) and set(record) == {"bytes", "sha256"})
    require(type(record["bytes"]) is int and 0 < record["bytes"] <= maximum)
    require(digest_id(record["sha256"]))


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "SEMANTIC_ASSET_JSON_DUPLICATE")
        result[key] = value
    return result


def finite_float(value):
    number = float(value)
    require(math.isfinite(number), "SEMANTIC_ASSET_JSON_NUMBER")
    return number


def read_json(path, maximum, budget, expected=None):
    """Allocate by actual verified bytes, never by a wide support cap."""
    path = safe(path)
    before = stamp(path)
    size = before[2]
    require(0 < size <= maximum, "SEMANTIC_ASSET_JSON_LIMIT")
    if expected is not None:
        descriptor(expected, maximum)
        require(size == expected["bytes"], "SEMANTIC_ASSET_DRIFT")
    budget.check()
    with path.open("rb") as stream:
        require(opened_stamp(stream) == before, "SEMANTIC_ASSET_DRIFT")
        raw = stream.read(size + 1)
        require(opened_stamp(stream) == before, "SEMANTIC_ASSET_DRIFT")
    require(len(raw) == size and stamp(path) == before, "SEMANTIC_ASSET_DRIFT")
    if expected is not None:
        require(hashlib.sha256(raw).hexdigest() == expected["sha256"], "SEMANTIC_ASSET_DRIFT")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_float=finite_float,
            parse_constant=lambda _: require(False, "SEMANTIC_ASSET_JSON_NUMBER"),
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ConsumptionError):
            raise
        raise ConsumptionError("SEMANTIC_ASSET_JSON") from exc
    budget.check()
    return value


def file_record(path, maximum, budget):
    path = safe(path)
    before = stamp(path)
    require(0 < before[2] <= maximum, "SEMANTIC_ASSET_BYTE_LIMIT")
    sha, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        require(opened_stamp(stream) == before, "SEMANTIC_ASSET_DRIFT")
        while data := stream.read(min(65536, before[2] - size + 1)):
            budget.check()
            size += len(data)
            require(size <= before[2], "SEMANTIC_ASSET_DRIFT")
            sha.update(data)
        require(opened_stamp(stream) == before, "SEMANTIC_ASSET_DRIFT")
    require(size == before[2] and stamp(path) == before, "SEMANTIC_ASSET_DRIFT")
    budget.check()
    return {"bytes": size, "sha256": sha.hexdigest()}


def seal(value):
    try:
        assets.verify_seal(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConsumptionError("SEMANTIC_ASSET_SEAL") from exc


def origin(directory, expected, budget, *, filename="manifest.json"):
    require(digest_id(expected), "SEMANTIC_ASSET_ID")
    directory = safe(directory)
    m = read_json(directory / filename, CAPS["manifest.json"], budget)
    seal(m)
    require(
        set(m)
        == {
            "version",
            "accepted",
            "shape",
            "binding",
            "chunks",
            "operations",
            "input_directory",
            "candidate_release",
            "new_classifier_fitted",
            "old_model_compatible",
            "files",
            "content_hash",
        }
    )
    require(
        isinstance(m["input_directory"], str)
        and len(m["input_directory"]) <= 256
        and m["input_directory"].startswith("runtime/")
    )
    require(m["content_hash"] == expected, "SEMANTIC_ASSET_ORIGIN_DRIFT")
    require(m.get("version") == PRODUCER and m.get("accepted") == COUNT)
    require(type(m["accepted"]) is int and m.get("shape") == [COUNT, WIDTH])
    require(all(type(value) is int for value in m["shape"]))
    require(
        all(
            m.get(k) is False
            for k in ("candidate_release", "old_model_compatible", "new_classifier_fitted")
        )
    )
    files = m.get("files")
    require(isinstance(files, dict) and set(files) == PAYLOADS)
    for name, record in files.items():
        descriptor(record, CAPS[name])
    binding = m.get("binding")
    require(
        isinstance(binding, dict)
        and set(binding)
        == {"inputs", "declaration", "checkpoint", "sources", "dependencies", "execution"}
    )
    require(all(digest_id(binding[k]) for k in ("inputs", "declaration", "checkpoint")))
    sources, dependencies = binding["sources"], binding["dependencies"]
    require(isinstance(sources, dict) and len(sources) == 18)
    require(
        all(
            isinstance(k, str)
            and 0 < len(k) <= 160
            and not k.startswith(("/", "."))
            and "\\" not in k
            and ".." not in k.split("/")
            and digest_id(v)
            for k, v in sources.items()
        )
    )
    require(
        isinstance(dependencies, dict)
        and set(dependencies) == {"numpy", "torch", "transformers", "safetensors", "tokenizers"}
    )
    require(all(isinstance(v, str) and 0 < len(v) <= 64 for v in dependencies.values()))
    execution = {
        "device": "cpu",
        "dtype": "float32",
        "threads": 4,
        "batch": 8,
        "tokens": 128,
        "code_windows_maximum": 32,
        "message_windows": 2,
        "pooling": "content-token masked mean, token-weighted windows; 4 role masks",
        "chunk_slices": [[a, min(a + 96, COUNT)] for a in range(0, COUNT, 96)],
        "window_selection": "contiguous complete selected code tokens; adaptive count",
    }
    require(assets.content_hash(binding["execution"]) == assets.content_hash(execution))
    chunks, operations = m.get("chunks"), m.get("operations")
    require(
        isinstance(chunks, list)
        and len(chunks) == 7
        and all(digest_id(x) for x in chunks)
        and len(set(chunks)) == 7
    )
    require(isinstance(operations, list) and len(operations) == 7)
    ids = []
    for operation in operations:
        require(
            isinstance(operation, dict)
            and set(operation)
            == {
                "operation_id",
                "process_id",
                "computed_new_forward",
                "model_frozen",
                "first_batch_repeat_exact",
            }
        )
        require(
            all(
                operation[k] is True
                for k in ("computed_new_forward", "model_frozen", "first_batch_repeat_exact")
            )
        )
        require(type(operation["process_id"]) is int and operation["process_id"] > 0)
        op = operation["operation_id"]
        require(isinstance(op, str) and re.fullmatch(r"[0-9a-f]{32}", op) is not None)
        ids.append(op)
    require(len(set(ids)) == 7)
    return m


def members(directory, manifest, budget):
    values = [
        read_json(directory / name, CAPS[name], budget, manifest["files"][name])
        for name in ("rows.json", "coverage.json")
    ]
    require(
        all(
            isinstance(x, dict)
            and set(x) == {"records"}
            and isinstance(x["records"], list)
            and len(x["records"]) == COUNT
            for x in values
        )
    )
    rows, coverage = (x["records"] for x in values)
    seen, partitions = set(), Counter()
    for index, (row, audit) in enumerate(zip(rows, coverage, strict=True)):
        budget.check()
        require(isinstance(row, dict) and set(row) == {"population_index", "member", "text_hash"})
        require(
            type(row["population_index"]) is int
            and row["population_index"] == index
            and digest_id(row["text_hash"])
        )
        member = row["member"]
        require(
            isinstance(member, dict)
            and set(member) == {"canonical_repository", "commit_id", "partition"}
        )
        repo, sha, partition = (
            member[k] for k in ("canonical_repository", "commit_id", "partition")
        )
        require(
            isinstance(repo, str)
            and len(repo) <= 160
            and re.fullmatch(r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*", repo) is not None
        )
        require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) is not None)
        require(partition in {"train", "validation"} and (repo, sha) not in seen)
        seen.add((repo, sha))
        partitions[partition] += 1
        require(isinstance(audit, dict) and set(audit) == {"population_index", "member", "roles"})
        require(
            type(audit["population_index"]) is int
            and audit["population_index"] == index
            and audit["member"] == member
        )
        roles = audit["roles"]
        require(isinstance(roles, dict) and set(roles) == set(ROLES))
        for role in ROLES:
            r = roles[role]
            require(
                isinstance(r, dict)
                and set(r)
                == {
                    "tokens",
                    "all_windows",
                    "covered_tokens",
                    "omitted_tokens",
                    "empty",
                    "selected_starts",
                }
            )
            require(
                all(
                    type(r[k]) is int
                    for k in ("tokens", "all_windows", "covered_tokens", "omitted_tokens")
                )
            )
            tokens = r["tokens"]
            require(0 <= tokens <= (252 if role == "message" else 4032))
            require(r["covered_tokens"] == tokens and r["omitted_tokens"] == 0)
            starts = list(range(0, tokens, 126))
            require(r["all_windows"] == len(starts) and r["selected_starts"] == starts)
            require(all(type(x) is int for x in r["selected_starts"]))
            require(type(r["empty"]) is bool and r["empty"] == (tokens == 0))
    require(dict(partitions) == {"train": 488, "validation": 172})
    return rows, coverage


def matrix(path, record, coverage, budget):
    """Validate bounded NPY header/length before streaming individual numeric rows."""
    descriptor(record, CAPS["embeddings.npy"])
    path = safe(path)
    before = stamp(path)
    require(before[2] == record["bytes"], "SEMANTIC_ASSET_NPY_LENGTH")
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        require(opened_stamp(stream) == before, "SEMANTIC_ASSET_DRIFT")
        prefix = stream.read(10)
        require(
            len(prefix) == 10 and prefix[:8] == b"\x93NUMPY\x01\x00", "SEMANTIC_ASSET_NPY_HEADER"
        )
        length = int.from_bytes(prefix[8:], "little")
        require(0 < length <= 4096, "SEMANTIC_ASSET_NPY_HEADER")
        require(before[2] == 10 + length + COUNT * WIDTH * 4, "SEMANTIC_ASSET_NPY_LENGTH")
        header = stream.read(length)
        require(len(header) == length and header.endswith(b"\n"), "SEMANTIC_ASSET_NPY_HEADER")
        try:
            syntax = ast.parse(header.decode("ascii").strip(), mode="eval")
            require(
                isinstance(syntax.body, ast.Dict) and len(syntax.body.keys) == 3,
                "SEMANTIC_ASSET_NPY_SCHEMA",
            )
            schema = ast.literal_eval(syntax)
        except ConsumptionError:
            raise
        except (ValueError, SyntaxError, UnicodeError, RecursionError) as exc:
            raise ConsumptionError("SEMANTIC_ASSET_NPY_HEADER") from exc
        require(isinstance(schema, dict) and set(schema) == {"descr", "fortran_order", "shape"})
        require(
            schema["descr"] == "<f4"
            and schema["fortran_order"] is False
            and schema["shape"] == (COUNT, WIDTH),
            "SEMANTIC_ASSET_NPY_SCHEMA",
        )
        require(all(type(x) is int for x in schema["shape"]))
        sha.update(prefix + header)
        for index in range(COUNT):
            budget.check()
            raw = stream.read(WIDTH * 4)
            require(len(raw) == WIDTH * 4, "SEMANTIC_ASSET_NPY_LENGTH")
            sha.update(raw)
            values = struct.unpack("<" + "f" * WIDTH, raw)
            require(all(math.isfinite(x) for x in values), "SEMANTIC_ASSET_NPY_NONFINITE")
            masks = tuple(float(coverage[index]["roles"][role]["tokens"] > 0) for role in ROLES)
            require(values[-4:] == masks, "SEMANTIC_ASSET_NPY_MASK")
            for ri, mask in enumerate(masks):
                if not mask:
                    require(not any(values[ri * 768 : (ri + 1) * 768]), "SEMANTIC_ASSET_NPY_EMPTY")
        require(stream.read(1) == b"", "SEMANTIC_ASSET_NPY_LENGTH")
        require(opened_stamp(stream) == before, "SEMANTIC_ASSET_DRIFT")
    require(sha.hexdigest() == record["sha256"] and stamp(path) == before, "SEMANTIC_ASSET_DRIFT")
    budget.check()


def payload(directory, manifest, budget):
    rows, coverage = members(directory, manifest, budget)
    matrix(directory / "embeddings.npy", manifest["files"]["embeddings.npy"], coverage, budget)
    return rows, coverage
