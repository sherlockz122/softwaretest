"""Count supervised local Git reads; compare complete records with a bounded cache."""

import argparse
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import digest
from packages.repositories.parser import NativeReader
from scripts.probe_causal_features import ROOT, checked_path


def environment(home):
    home.mkdir()
    values = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    }
    values.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home),
        TEMP=str(home),
        TMP=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_AUTHOR_NAME="Probe",
        GIT_AUTHOR_EMAIL="probe@example.invalid",
        GIT_COMMITTER_NAME="Probe",
        GIT_COMMITTER_EMAIL="probe@example.invalid",
    )
    configs = {"protocol.allow": "never", "core.hooksPath": str(home), "core.fsmonitor": "false"}
    if os.name == "nt":
        configs["core.longpaths"] = "true"
    values["GIT_CONFIG_COUNT"] = str(len(configs))
    for index, (key, value) in enumerate(configs.items()):
        values[f"GIT_CONFIG_KEY_{index}"] = key
        values[f"GIT_CONFIG_VALUE_{index}"] = value
    return values


def fixture(bare, env, count):
    def git(*args, data=None):
        return (
            subprocess.check_output(
                [shutil.which("git"), "--literal-pathspecs", "-C", str(bare), *args],
                env=env,
                input=data,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            .strip()
            .decode("ascii")
        )

    bare.mkdir()
    git("init", "--bare", "--quiet")
    # Four recurring immutable blobs model repeated edits/reverts, not training data.
    blobs = [git("hash-object", "-w", "--stdin", data=f"value={i}\n".encode()) for i in range(4)]
    trees = [git("mktree", data=f"100644 blob {blob}\tcode.py\n".encode()) for blob in blobs]
    shas = []
    for index in range(count):
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{1577836800 + index} +0000"
        parent = ["-p", shas[-1]] if shas else []
        shas.append(git("commit-tree", trees[index % 4], *parent, data=b"Fix probe\n"))
    return shas


class CountedReader(NativeReader):
    def __init__(self, *args):
        super().__init__(*args)
        self.counts = Counter()

    def command(self, arguments, *args, **kwargs):
        category = " ".join(arguments[:2]) if arguments[0] == "cat-file" else arguments[0]
        self.counts[category] += 1
        return super().command(arguments, *args, **kwargs)


def probe(count, optimized=False):
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("READ_PROBE_COMMIT_LIMIT")
    work = checked_path(ROOT / "runtime/temp")
    work.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="feature-read-", dir=work) as name:
        root = Path(name)
        env = environment(root / "home")
        shas = fixture(root / "source.git", env, count)
        attempt = root / "attempt"
        attempt.mkdir()

        def tick():
            if shutil.disk_usage(ROOT).free < 2 * 1024**3:
                raise ValueError("READ_PROBE_CAPACITY_UNSAFE")

        reader = CountedReader(root / "source.git", attempt, env, tick, time.monotonic() + 300)
        started = time.perf_counter()
        if optimized:
            from packages.mining.feature_reads import cached_blobs

            with cached_blobs(reader) as cache:
                records = [reader.record(sha) for sha in shas]
                inputs = [freeze_native_record(reader, record) for record in records]
            cache_metrics = cache.metrics
        else:
            records = [reader.record(sha) for sha in shas]
            inputs = [freeze_native_record(reader, record) for record in records]
            cache_metrics = None
        return {
            "commits": count,
            "optimized": optimized,
            "wall_seconds": round(time.perf_counter() - started, 6),
            "commands": dict(sorted(reader.counts.items())),
            "total_commands": sum(reader.counts.values()),
            "native_digest": digest(records),
            "input_digest": digest([asdict(row) for row in inputs]),
            "cache": cache_metrics,
            "scope": "real local Git fixture; not public label certification or model accuracy",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commits", type=int, nargs="+", default=[20, 120])
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = checked_path(args.output)
    if not output.is_relative_to(ROOT / "runtime/acceptance") or output.exists():
        raise ValueError("READ_PROBE_OUTPUT_INVALID")
    results = []
    for count in args.commits:
        baseline = probe(count)
        row = {"baseline": baseline}
        if args.compare:
            optimized = probe(count, True)
            assert baseline["native_digest"] == optimized["native_digest"]
            assert baseline["input_digest"] == optimized["input_digest"]
            row.update(optimized=optimized, exact_equal=True)
        results.append(row)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results))


if __name__ == "__main__":
    main()
