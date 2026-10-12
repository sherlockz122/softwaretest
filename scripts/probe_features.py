"""Synthetic linear-history resource probe, never a model accuracy experiment."""

import argparse
import json
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

from packages.mining.features import (
    FEATURE_VERSION,
    RULE_HASH,
    CommitInput,
    FileDelta,
    Snapshot,
    compute,
    quality,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[200, 1000])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if not output.is_relative_to(root / "runtime/acceptance") or any(n < 1 for n in args.counts):
        parser.error("Positive counts and an output inside runtime/acceptance are required")
    measurements = []
    for count in args.counts:
        tracemalloc.start()
        started = time.perf_counter()
        records = tuple(
            CommitInput(
                f"{i + 1:040x}",
                (f"{i:040x}",) if i else (),
                datetime(2020, 1, 1) + timedelta(seconds=i),
                "a" * 64,
                (
                    FileDelta(
                        "src/a.py" if i else None,
                        "src/a.py",
                        "M" if i else "A",
                        1,
                        1 if i else 0,
                        3 if i else 0,
                        old_mode="100644" if i else "000000",
                    ),
                ),
            )
            for i in range(count)
        )
        outputs = compute(Snapshot("synthetic-probe", records[-1].sha, records))
        report = quality(outputs)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        measurements.append(
            {
                "commits": count,
                "seconds_with_tracemalloc": round(elapsed, 3),
                "peak_python_mib": round(peak / 1024**2, 3),
                "source_digest": outputs[-1].source_digest,
                "last_vector": outputs[-1].as_dict(),
                "quality": report,
            }
        )
        print(
            f"PASS synthetic {count} commits, {elapsed:.3f}s, {peak / 1024**2:.3f} MiB Python peak"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "feature_version": FEATURE_VERSION,
                "rule_hash": RULE_HASH,
                "scope": "synthetic linear history; Python allocations only; no accuracy claim",
                "measurements": measurements,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
