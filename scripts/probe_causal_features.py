"""Read a bounded local Git window; write causal features and an honest replay deficit."""

import argparse
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from packages.mining.causal_features import compute_causal, freeze_message
from packages.mining.feature_inputs import freeze_native_record
from packages.mining.features import HISTORY, Snapshot, digest
from packages.mining.historical_replay import (
    ReplayOutcome,
    ReplayPolicy,
    ReplaySource,
    audit_replay,
)
from packages.repositories.parser import NativeReader, sha_valid
from scripts.fetch_public_dataset import ROOT, runtime_path, safe_file


def checked_path(value):
    absolute = value.absolute()
    for part in (absolute, *absolute.parents):
        if part.exists() and (
            part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("REPLAY_PATH_LINK")
        if part == ROOT:
            break
    return runtime_path(absolute)


def probe(bare, head, repository, acquired_at, count=3):
    bare = checked_path(bare)
    if any(
        (bare / name).exists()
        for name in ("objects/info/alternates", "objects/info/http-alternates")
    ):
        raise ValueError("REPLAY_GIT_ALTERNATES")
    if not 1 <= count <= 1000:
        raise ValueError("REPLAY_COMMIT_LIMIT")
    sha_valid(head.encode("ascii"))
    acquired = datetime.fromisoformat(acquired_at)
    if acquired.tzinfo is None or acquired > datetime.now(UTC):
        raise ValueError("REPLAY_ACQUISITION_INVALID")
    acquired = acquired.astimezone(UTC).replace(tzinfo=None)
    work = checked_path(ROOT / "runtime/temp")
    work.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="causal-probe-", dir=work) as directory:
        attempt = Path(directory)
        home = attempt / "home"
        home.mkdir()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
        }
        environment.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home),
            TEMP=str(home),
            TMP=str(home),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_LAZY_FETCH="1",
            GIT_NO_REPLACE_OBJECTS="1",
        )
        config = {"protocol.allow": "never", "core.hooksPath": str(home), "core.fsmonitor": "false"}
        if os.name == "nt":
            config["core.longpaths"] = "true"
        environment["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (key, value) in enumerate(config.items()):
            environment[f"GIT_CONFIG_KEY_{i}"] = key
            environment[f"GIT_CONFIG_VALUE_{i}"] = value

        def tick():
            if shutil.disk_usage(ROOT).free < 2 * 1024**3:
                raise ValueError("REPLAY_CAPACITY_UNSAFE")

        reader = NativeReader(bare, attempt, environment, tick, time.monotonic() + 300)
        plan = reader.plan(head, count)
        records = [reader.record(sha) for sha in plan]
        inputs = tuple(freeze_native_record(reader, record) for record in records)
        # A deliberately bounded window never pretends to prove complete history.
        snapshot = Snapshot(repository, head, inputs, history_coverage="recent_window")
        run = compute_causal(
            snapshot, tuple(freeze_message(record) for record in records), tick=tick
        )
        times = [commit.event_time for commit in inputs]
        source = ReplaySource(
            repository,
            head,
            acquired,
            digest({"head": head, "acquired": acquired}),
            run.content_hash,
            min(times),
            max(times),
            False,
            True,
        )
        result = audit_replay(
            run,
            tuple(ReplayOutcome(commit.sha) for commit in inputs),
            source,
            ReplayPolicy(max(times)),
            tick=tick,
        )
    return {
        "scope": (
            "real Git bounded feature check; no complete negative observation or accuracy claim"
        ),
        "acquired_at": acquired.isoformat() + "Z",
        "head_sha": head,
        "features": run.as_dict(),
        "feature_content_hash": run.content_hash,
        "replay": result.as_dict(),
        "replay_content_hash": result.content_hash,
        "history_missing_expected": list(HISTORY),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bare", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--acquired-at", required=True)
    parser.add_argument("--commits", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bare, output = checked_path(args.bare), checked_path(args.output)
    if (
        not output.is_relative_to(ROOT / "runtime/acceptance")
        or output.is_relative_to(bare)
        or bare.is_relative_to(output.parent)
    ):
        raise ValueError("REPLAY_OUTPUT_SOURCE_COLLISION")
    report = probe(bare, args.head, args.repository, args.acquired_at, args.commits)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_file(output.parent, output.name + ".part")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(
        json.dumps(
            {
                "commits": len(report["features"]["vectors"]),
                "feature_content_hash": report["feature_content_hash"],
                "labels": report["replay"]["labels"],
                "product_training_ready": False,
            }
        )
    )


if __name__ == "__main__":
    main()
