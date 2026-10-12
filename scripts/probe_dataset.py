"""Synthetic maturity resource probe, not a data quality or accuracy experiment."""

import argparse
import json
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

from packages.mining.dataset import Evidence, Observation, Policy, audit, rule_hash
from packages.mining.features import FEATURES, CommitInput, FeatureVector, FileDelta, Snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if not output.is_relative_to(Path(__file__).resolve().parents[1] / "runtime/acceptance"):
        parser.error("Output must be inside runtime/acceptance")
    rows = []
    for count in (200, 1000, 3000):
        tracemalloc.start()
        started = time.perf_counter()
        records = tuple(
            CommitInput(
                f"{i + 1:040x}",
                (f"{i:040x}",) if i else (),
                datetime(2020, 1, 1) + timedelta(days=i),
                "a" * 64,
                (FileDelta("a.py", "a.py", "M", 1, 1, 3),),
            )
            for i in range(count)
        )
        at = records[-1].event_time + timedelta(days=100)
        vectors = tuple(
            FeatureVector(c.sha, c.event_time, "b" * 64, tuple((f, 1) for f in FEATURES), ())
            for c in records
        )
        observation = Observation(
            "synthetic",
            records[-1].sha,
            at,
            at,
            at,
            "c" * 64,
            tuple(
                Evidence(c.sha, at, False, "unreviewed", 0, True, True, True, "not_candidate")
                for c in records
            ),
        )
        result = audit(
            Snapshot("synthetic", records[-1].sha, records), vectors, (observation,), Policy(at)
        )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rows.append(
            dict(
                commits=count,
                seconds_with_tracemalloc=round(time.perf_counter() - started, 3),
                peak_python_mib=round(peak / 1024**2, 3),
                ready=result["ready"],
                label_counts=result["label_counts"],
                deficits=result["deficits"],
            )
        )
        print(json.dumps(rows[-1]))
    output.write_text(
        json.dumps(
            dict(
                scope="synthetic linear single full observation; Python allocations only",
                rule_hash=rule_hash(),
                measurements=rows,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
