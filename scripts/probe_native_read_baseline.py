"""Bounded offline native phase measurements; never certify DB or model throughput."""

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import threading
import time
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil

from packages.mining import public_assets as assets
from packages.mining.causal_features import compute_causal, freeze_message
from packages.mining.feature_inputs import freeze_native_record
from packages.mining.feature_reads import cached_blobs
from packages.mining.features import Snapshot, compute, digest
from packages.mining.indexed_features import compute_indexed
from packages.repositories.parser import NativeReader, sha_valid
from packages.semantic.artifacts import sources
from scripts.probe_feature_reads import environment, fixture

VERSION = "native-read-baseline-v1"
SOURCE_NAMES = [
    "scripts/probe_native_read_baseline.py",
    "scripts/probe_feature_reads.py",
    "packages/repositories/parser.py",
    "packages/repositories/parse_policy.py",
    "packages/mining/feature_inputs.py",
    "packages/mining/feature_reads.py",
    "packages/mining/features.py",
    "packages/mining/indexed_features.py",
    "packages/mining/causal_features.py",
    "uv.lock",
]


def policy(counts, repeats):
    if (
        type(repeats) is not int
        or not 3 <= repeats <= 5
        or not counts
        or len(counts) > 3
        or any(type(n) is not int or not 1 <= n <= 100 for n in counts)
        or list(counts) != sorted(set(counts))
    ):
        raise ValueError("NATIVE_MEASURE_POLICY")


def checked_path(path):
    path = assets.safe_path(Path(path))
    if not path.is_relative_to(assets.ROOT / "runtime"):
        raise ValueError("NATIVE_MEASURE_PATH")
    return path


def source_binding(source):
    source = checked_path(source)
    receipt = assets.read_json(source / "acquisition.json", lambda: None)
    assets.verify_seal(receipt)
    acquired = datetime.fromisoformat(receipt["acquired_at"])
    if (
        receipt.get("version") != "semantic-git-acquisition-v1"
        or not isinstance(receipt.get("repository"), str)
        or not receipt["repository"]
        or acquired.tzinfo is None
        or acquired > datetime.now(UTC)
    ):
        raise ValueError("NATIVE_MEASURE_SOURCE_METADATA")
    sha_valid(receipt["head"].encode("ascii"))
    bare = source / "source.git"
    if not bare.is_dir() or any(
        (bare / name).exists()
        for name in [
            "shallow",
            "info/grafts",
            "objects/info/alternates",
            "objects/info/http-alternates",
            "refs/replace",
        ]
    ):
        raise ValueError("NATIVE_MEASURE_STORAGE")
    checked_path(bare)
    return bare, receipt


class SampledRSS:
    """50 ms observed tree RSS, not an exact process creation or memory profiler."""

    def __init__(self):
        self.peak = 0
        self.samples = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.observe, daemon=True)

    def observe(self):
        parent = psutil.Process()
        while not self.stop.is_set():
            total = 0
            try:
                processes = [parent, *parent.children(recursive=True)]
            except psutil.Error:
                processes = [parent]
            for process in processes:
                try:
                    total += process.memory_info().rss
                except psutil.Error:
                    pass
            self.peak = max(self.peak, total)
            self.samples += 1
            self.stop.wait(0.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)


class Measurement:
    def __init__(self, deadline, memory):
        self.deadline, self.memory = deadline, memory
        self.current = "setup"
        self.phases = {}

    def phase(self):
        return self.phases.setdefault(
            self.current,
            {"seconds": 0.0, "commands": {}, "ticks": 0, "tick_seconds": 0.0},
        )

    def tick(self):
        start = time.perf_counter()
        row = self.phase()
        row["ticks"] += 1
        try:
            if time.monotonic() >= self.deadline:
                raise ValueError("NATIVE_MEASURE_DEADLINE")
            if self.memory.peak > 2 * 1024**3:
                raise ValueError("NATIVE_MEASURE_RSS")
            if shutil.disk_usage(assets.ROOT).free < 2.5 * 1024**3:
                raise ValueError("NATIVE_MEASURE_CAPACITY")
        finally:
            row["tick_seconds"] += time.perf_counter() - start

    @contextmanager
    def stage(self, name):
        previous, self.current = self.current, name
        start = time.perf_counter()
        try:
            self.tick()
            yield
            self.tick()
        finally:
            self.phase()["seconds"] += time.perf_counter() - start
            self.current = previous


class MeasuredReader(NativeReader):
    def __init__(self, *args, measurement):
        super().__init__(*args)
        self.measurement = measurement

    def command(self, arguments, *args, **kwargs):
        if arguments[0] == "cat-file":
            name = " ".join(arguments[:2])
        elif arguments[0] == "diff-tree":
            name = "diff-tree " + next(
                (flag for flag in ["--raw", "--numstat", "-p"] if flag in arguments), "other"
            )
        else:
            name = arguments[0]
        row = self.measurement.phase()["commands"].setdefault(
            name, {"calls": 0, "inclusive_seconds": 0.0}
        )
        row["calls"] += 1
        start = time.perf_counter()
        try:
            return super().command(arguments, *args, **kwargs)
        finally:
            row["inclusive_seconds"] += time.perf_counter() - start


def trial(bare, head, repository, count, cached, root, deadline, memory, members=None):
    with TemporaryDirectory(dir=root, prefix="trial-") as folder:
        work = Path(folder)
        env = environment(work / "home")
        extra = {
            "safe.directory": str(bare),
            "diff.external": "",
            "core.attributesFile": os.devnull,
        }
        offset = int(env["GIT_CONFIG_COUNT"])
        for index, (key, value) in enumerate(extra.items(), offset):
            env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
        env["GIT_CONFIG_COUNT"] = str(offset + len(extra))
        metric = Measurement(deadline, memory)
        reader = MeasuredReader(
            bare, work, env, metric.tick, min(deadline, time.monotonic() + 300), measurement=metric
        )
        with metric.stage("plan"):
            if reader.command(["rev-parse", "--is-bare-repository"]).strip() != b"true":
                raise ValueError("NATIVE_MEASURE_NOT_BARE")
            if reader.command(["rev-parse", head + "^{commit}"]).strip().decode() != head:
                raise ValueError("NATIVE_MEASURE_HEAD")
            planned = reader.plan(head, count)
            if not planned or members is not None and planned != members:
                raise ValueError("NATIVE_MEASURE_POPULATION_DRIFT")
        with cached_blobs(reader) if cached else nullcontext() as cache:
            with metric.stage("record"):
                records = [reader.record(sha) for sha in planned]
            if sum(len(row["files"]) for row in records) > 10000:
                raise ValueError("NATIVE_MEASURE_FILE_LIMIT")
            with metric.stage("native_freeze_input"):
                inputs = tuple(freeze_native_record(reader, row) for row in records)
            cache_metrics = dict(cache.metrics) if cached else None
        snapshot = Snapshot(repository, head, inputs, history_coverage="recent_window")
        with metric.stage("numeric_legacy"):
            legacy = compute(snapshot, tick=metric.tick)
        with metric.stage("numeric_indexed"):
            indexed = compute_indexed(snapshot, tick=metric.tick)
        if legacy != indexed:
            raise ValueError("NATIVE_MEASURE_NUMERIC_DRIFT")
        with metric.stage("numeric_causal"):
            causal = compute_causal(
                snapshot, tuple(freeze_message(r) for r in records), tick=metric.tick
            )
        native_seconds = sum(
            metric.phases[p]["seconds"] for p in ["plan", "record", "native_freeze_input"]
        )
        return {
            "cached": cached,
            "members": planned,
            "member_digest": digest(planned),
            "actual_commits": len(planned),
            "files": sum(len(r["files"]) for r in records),
            "file_statuses": dict(
                Counter(f["content_status"] for r in records for f in r["files"])
            ),
            "changes": dict(Counter(f["change_type"] for r in records for f in r["files"])),
            "native_digest": digest(records),
            "input_digest": digest([asdict(row) for row in inputs]),
            "numeric_digest": digest([row.as_dict() for row in indexed]),
            "causal_digest": causal.content_hash,
            "native_seconds": native_seconds,
            "phases": metric.phases,
            "cache": cache_metrics,
        }


def compare(trials):
    for key in ["members", "native_digest", "input_digest", "numeric_digest", "causal_digest"]:
        if any(row[key] != trials[0][key] for row in trials):
            raise ValueError("NATIVE_MEASURE_EQUIVALENCE_" + key.upper())
    summary = {}
    for cached in [False, True]:
        selected = [r for r in trials if r["cached"] is cached]
        times = [r["native_seconds"] for r in selected]
        if len(times) < 3:
            raise ValueError("NATIVE_MEASURE_REPEATS_MISSING")
        summary["cached" if cached else "uncached"] = {
            "trials": len(times),
            "median_seconds": statistics.median(times),
            "range_seconds": [min(times), max(times)],
        }
    return summary | {"all_exact": True, "member_digest": trials[0]["member_digest"]}


def probe(output, counts, repeats=3, source=None):
    policy(counts, repeats)
    output = checked_path(output)
    if not output.is_relative_to(assets.ROOT / "runtime/acceptance") or output.exists():
        raise ValueError("NATIVE_MEASURE_OUTPUT")
    bare, receipt = source_binding(source) if source is not None else (None, None)
    if bare is not None and (output.is_relative_to(bare) or bare.is_relative_to(output)):
        raise ValueError("NATIVE_MEASURE_COLLISION")
    output.mkdir(parents=True)
    started, deadline = time.monotonic(), time.monotonic() + 1800
    code = sources(SOURCE_NAMES)
    metadata = {
        "version": VERSION,
        "source": receipt,
        "source_scope": "fixed local source" if source else "inert recurring-blob Git fixture",
        "counts": counts,
        "repeats": repeats,
        "sources": code,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "psutil": psutil.__version__,
        "git": subprocess.check_output([shutil.which("git"), "--version"], timeout=10)
        .decode()
        .strip(),
        "limits": {"native_seconds": 300, "total_seconds": 1800, "sampled_rss_bytes": 2 * 1024**3},
        "unmeasured": [
            "online_v1_v2_database",
            "atomic_database_freeze",
            "locks",
            "worker_e2e",
            "semantic_reader",
        ],
        "os_cache_controlled": False,
        "sampling": (
            "50ms self+descendant RSS observed lower bound; timing includes observer overhead"
        ),
        "timing": (
            "command seconds include waits and tick; ticks overlap command/phase time, not additive"
        ),
        "full_product_p1_complete": False,
        "candidate_release": False,
    }
    assets.write_json(output / "declaration.json", assets.sealed(metadata))
    windows = []
    try:
        with SampledRSS() as memory, TemporaryDirectory(dir=output, prefix="work-") as folder:
            root = Path(folder)
            if source is None:
                env = environment(root / "fixture-home")
                bare = root / "fixture.git"
                shas = fixture(bare, env, max(counts))
                head, repository = shas[-1], "inert-native-measurement"
            else:
                head, repository = receipt["head"], receipt["repository"]
            with (output / "trials.jsonl").open("x", encoding="utf-8") as stream:
                for count in counts:
                    rows, members = [], None
                    for repeat in range(repeats):
                        for cached in [False, True] if repeat % 2 == 0 else [True, False]:
                            row = trial(
                                bare,
                                head,
                                repository,
                                count,
                                cached,
                                root,
                                deadline,
                                memory,
                                members,
                            )
                            members = row["members"]
                            row.update(window=count, repeat=repeat)
                            stream.write(json.dumps(row, allow_nan=False) + "\n")
                            stream.flush()
                            if stream.tell() > 64 * 1024**2:
                                raise ValueError("NATIVE_MEASURE_OUTPUT_LIMIT")
                            rows.append(row)
                            print(
                                json.dumps(
                                    {
                                        "window": count,
                                        "repeat": repeat,
                                        "cached": cached,
                                        "native_seconds": row["native_seconds"],
                                    }
                                ),
                                flush=True,
                            )
                    windows.append({"requested": count, "summary": compare(rows)})
                    if (
                        max(r["native_seconds"] for r in rows) > 90
                        or deadline - time.monotonic() < 600
                    ):
                        break
            if sources(SOURCE_NAMES) != code or source and source_binding(source)[1] != receipt:
                raise ValueError("NATIVE_MEASURE_BINDING_DRIFT")
            result = metadata | {
                "windows": windows,
                "not_measured_counts": [
                    n for n in counts if n not in [w["requested"] for w in windows]
                ],
                "sampled_peak_rss_bytes": memory.peak,
                "rss_samples": memory.samples,
                "seconds": time.monotonic() - started,
            }
            assets.write_json(output / "summary.json", assets.sealed(result))
            return result
    except BaseException as exc:
        assets.write_json(
            output / "failed.json",
            {
                "type": type(exc).__name__,
                "seconds": time.monotonic() - started,
                "completed_windows": windows,
            },
        )
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--counts", type=int, nargs="+", default=[20, 50, 100])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.output, args.counts, args.repeats, args.source)
    print(json.dumps({"windows": result["windows"], "seconds": result["seconds"]}))


if __name__ == "__main__":
    main()
