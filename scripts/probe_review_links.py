"""Probe displayed raw candidate links in an existing local Git; never fetch or relabel."""

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from packages.mining.asset_reviews import case_history
from packages.mining.public_assets import (
    ROOT,
    AssetError,
    Budget,
    atomic_json,
    read_json,
    safe_path,
    sealed,
    utc_time,
)
from packages.mining.source_evidence import content_hash
from packages.repositories.parser import NativeReader, sha_valid


def probe(directory, case_id, bare, acquisition, output, *, tick=None):
    budget = Budget(tick=tick)
    history = case_history(directory, case_id, tick=budget.check)
    output = safe_path(output)
    asset_store = safe_path(ROOT / history["initial"]["binding"]["asset_path"]).parent
    if output.is_relative_to(asset_store):
        raise AssetError("REVIEW_OUTPUT_COLLISION")
    packet = history["records"][0]["packet"]
    receipt = read_json(acquisition, budget.check)
    if receipt.get("repository") != packet["project"]:
        raise AssetError("REVIEW_GIT_REPOSITORY_CONFLICT")
    utc_time(receipt.get("acquired_at"))
    bare = safe_path(bare)
    if not bare.is_dir():
        raise AssetError("REVIEW_GIT_STORAGE_INVALID")
    repository_files, repository_bytes = 0, 0
    for parent, dirs, files in os.walk(bare, followlinks=False):
        budget.check()
        for name in [*dirs, *files]:
            path = safe_path(Path(parent) / name)
            repository_files += 1
            if path.is_file():
                repository_bytes += path.stat().st_size
            if repository_files > 10000 or repository_bytes > 256 * 1024**2:
                raise AssetError("REVIEW_GIT_STORAGE_LIMIT")
    if any(
        safe_path(bare / name).exists()
        for name in ("objects/info/alternates", "objects/info/http-alternates", "info/grafts")
    ):
        raise AssetError("REVIEW_GIT_ALTERNATES")
    links = packet.get("display_links")
    if not isinstance(links, list) or len(links) > 8:
        raise AssetError("REVIEW_GIT_LINK_LIMIT")
    temporary = safe_path(ROOT / "runtime/temp")
    temporary.mkdir(parents=True, exist_ok=True)
    results = []
    with TemporaryDirectory(prefix="review-git-", dir=temporary) as work:
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
        }
        environment.update(
            HOME=work,
            XDG_CONFIG_HOME=work,
            TEMP=work,
            TMP=work,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_LAZY_FETCH="1",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_OPTIONAL_LOCKS="0",
        )
        config = {
            "protocol.allow": "never",
            "safe.directory": str(bare),
            "core.hooksPath": work,
            "core.fsmonitor": "false",
            "core.longpaths": "true",
        }
        environment["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (k, v) in enumerate(config.items()):
            environment[f"GIT_CONFIG_KEY_{i}"] = k
            environment[f"GIT_CONFIG_VALUE_{i}"] = v
        reader = NativeReader(bare, Path(work), environment, budget.check, time.monotonic() + 120)
        shallow = (
            reader.command(["rev-parse", "--is-shallow-repository"], limit=1024).strip() == b"true"
        )
        for item in links:
            bug, fix = packet["commit_id"], item["fix_hash"]
            for sha in (bug, fix):
                sha_valid(sha.encode("ascii"))
            clocks = {}
            for role, sha in (("bug", bug), ("fix", fix)):
                present = (
                    reader.command(
                        ["cat-file", "-e", sha + "^{commit}"], codes=(0, 128), exit_code=True
                    )
                    == 0
                )
                clock = None
                if present:
                    fields = (
                        reader.command(["show", "-s", "--format=%H %at %ct", sha, "--"], limit=1024)
                        .decode("ascii")
                        .strip()
                        .split()
                    )
                    if len(fields) != 3 or fields[0] != sha:
                        raise AssetError("REVIEW_GIT_CLOCK_INVALID")
                    clock = {"author_epoch": int(fields[1]), "committer_epoch": int(fields[2])}
                clocks[role] = {"sha": sha, "present": present, "clock": clock}
            relation = "unknown_missing_objects"
            if all(c["present"] for c in clocks.values()):
                if bug == fix:
                    relation = "self_link_not_strict"
                else:
                    code = reader.command(
                        ["merge-base", "--is-ancestor", bug, fix], codes=(0, 1, 128), exit_code=True
                    )
                    # The non-shallow flag does not independently certify graph completeness.
                    relation = "strict_ancestor" if code == 0 else "unknown_unverified_graph"
            results.append(
                {
                    "raw_link": item,
                    "objects": clocks,
                    "ancestry": relation,
                    "raw_clock_matches": {
                        role: None
                        if clocks[role]["clock"] is None
                        else clocks[role]["clock"]["committer_epoch"] == item[role + "_epoch"]
                        for role in ("bug", "fix")
                    },
                }
            )
    binding = history["initial"]["binding"]
    value = sealed(
        {
            "version": "native-link-probe-v1",
            "asset_id": binding["asset_id"],
            "report_hash": binding["report_hash"],
            "case_id": case_id,
            "observed_at": datetime.now(UTC).isoformat(),
            "git_acquisition_at": receipt["acquired_at"],
            "git_acquisition_hash": content_hash(receipt),
            "shallow": shallow,
            "results": results,
            "display_only": True,
            "display_links_truncated": packet["display_links_truncated"],
            "new_git_download": False,
            "historical_available_at": None,
            "negative_coverage_complete": False,
            "label_certified": False,
        }
    )
    atomic_json(output, value, check=budget.check)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("case_id")
    parser.add_argument("--bare", type=Path, required=True)
    parser.add_argument("--acquisition", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = probe(args.directory, args.case_id, args.bare, args.acquisition, args.output)
        print(
            json.dumps(
                {
                    "case_id": value["case_id"],
                    "links": len(value["results"]),
                    "relations": [r["ancestry"] for r in value["results"]],
                    "label_certified": False,
                }
            )
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(
            1, (str(exc) if isinstance(exc, AssetError) else "REVIEW_GIT_INPUT_INVALID") + "\n"
        )


if __name__ == "__main__":
    main()
