"""Synthetic resource/equivalence probe; never training or accuracy evidence."""

import argparse
import json
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

from packages.mining.features import CommitInput, FileDelta, Snapshot, compute, digest
from packages.mining.indexed_features import compute_indexed, rule_hash


def sample(count, sparse):
    files = [f"src/file{i}.py" for i in range(count if sparse else 1)]
    inputs = tuple(
        CommitInput(
            f"{i + 1:040x}",
            (f"{i:040x}",) if i else (),
            datetime(2020, 1, 1) + timedelta(seconds=i),
            f"{i % 8:064x}",
            tuple(
                FileDelta(None, path, "A", 1, 0, 0)
                if not i
                else FileDelta(path, path, "M", 1, 1, 3)
                for path in (files if not i else [files[i % len(files)]])
            ),
        )
        for i in range(count)
    )
    return Snapshot("synthetic-index-probe", inputs[-1].sha, inputs)


def measure(snapshot, implementation):
    ticks = 0

    def tick():
        nonlocal ticks
        ticks += 1

    started = time.perf_counter()
    output = implementation(snapshot, tick=tick)
    return output, {"seconds": time.perf_counter() - started, "supervisor_ticks": ticks}


def probe(counts):
    measurements = []
    for count in counts:
        for sparse in (True, False):
            snapshot = sample(count, sparse)
            baseline, legacy = measure(snapshot, compute)
            indexed, optimized = measure(snapshot, compute_indexed)
            assert indexed == baseline
            tracemalloc.start()
            assert compute_indexed(snapshot) == baseline
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            row = {
                "commits": count,
                "layout": "sparse" if sparse else "dense",
                "files": sum(len(c.files) for c in snapshot.commits),
                "exact_equivalence": True,
                "legacy": legacy,
                "indexed": optimized,
                "indexed_peak_python_mib": peak / 1024**2,
                "source_digest": indexed[0].source_digest,
                "vectors_digest": digest([v.as_dict() for v in indexed]),
            }
            measurements.append(row)
            print(json.dumps(row), flush=True)
    return {
        "scope": "synthetic numeric kernel only; Python allocations not RSS/Git/DB/end-to-end",
        "limits_unchanged": True,
        "rule_hash": rule_hash(),
        "measurements": measurements,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[200, 1000])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "runtime/acceptance"
    output = args.output.resolve()
    if not output.is_relative_to(root) or any(not 1 <= n <= 1000 for n in args.counts):
        parser.error("Counts 1..1000 and an output under runtime/acceptance required")
    if any(
        p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400
        for p in (output, *output.parents)
        if p.exists()
    ):
        parser.error("Output path must not contain links/reparse points")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(probe(args.counts), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
