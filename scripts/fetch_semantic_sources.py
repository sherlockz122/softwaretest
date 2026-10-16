"""Fetch one fixed public Git without checkout or upstream code execution."""

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

from packages.mining import public_assets as assets
from packages.repositories.clone import terminate
from packages.repositories.storage import directory_bytes

HEAD = "13def043d6f00d7adbfa14b9b083141afc82b82a"


def acquire():
    root = assets.safe_path(assets.ROOT / "runtime/semantic-sources")
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("activemq-" + HEAD)
    if target.exists():
        receipt = assets.read_json(target / "acquisition.json", lambda: None)
        assets.verify_seal(receipt)
        if receipt["head"] != HEAD:
            raise ValueError("SEMANTIC_SOURCE_CONFLICT")
        return target
    attempt = assets.safe_path(root / (".acquire-" + uuid4().hex))
    attempt.mkdir()
    template = attempt / "empty-template"
    template.mkdir()
    bare = attempt / "source.git"
    environment = dict(os.environ)
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
    )
    command = [
        shutil.which("git"),
        "-c",
        "core.longpaths=true",
        "-c",
        "core.hooksPath=" + str(template),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "gc.auto=0",
        "-c",
        "fetch.fsckObjects=true",
        "-c",
        "transfer.fsckObjects=true",
        "-c",
        "transfer.bundleURI=false",
        "-c",
        "fetch.uriprotocols=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.ssh.allow=always",
    ]
    subprocess.run(
        command + ["init", "--bare", "--template=" + str(template), str(bare)],
        env=environment,
        check=True,
        capture_output=True,
        timeout=15,
    )
    started = time.monotonic()
    with (attempt / "fetch.log").open("xb") as log:
        process = subprocess.Popen(
            command
            + [
                "-C",
                str(bare),
                "fetch",
                "--quiet",
                "--no-tags",
                "ssh://git@ssh.github.com:443/apache/activemq.git",
                HEAD + ":refs/heads/pinned",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            while process.poll() is None:
                if (
                    time.monotonic() - started > 300
                    or directory_bytes(attempt) > 1024**3
                    or shutil.disk_usage(root).free < 2.5 * 1024**3
                ):
                    raise ValueError("SEMANTIC_FETCH_RESOURCE_LIMIT")
                time.sleep(1)
            if process.returncode:
                raise ValueError("SEMANTIC_FETCH_FAILED")
        finally:
            terminate(process)
    actual = (
        subprocess.check_output(
            command + ["-C", str(bare), "rev-parse", "refs/heads/pinned"],
            env=environment,
            timeout=15,
        )
        .decode()
        .strip()
    )
    if actual != HEAD:
        raise ValueError("SEMANTIC_FETCH_DRIFT")
    receipt = assets.sealed(
        {
            "version": "semantic-git-acquisition-v1",
            "repository": "apache/activemq",
            "head": HEAD,
            "acquired_at": datetime.now(UTC).isoformat(),
            "bytes": directory_bytes(bare),
            "main_reachable_history_only": True,
            "all_branch_history_certified": False,
            "checkout": False,
            "upstream_code_executed": False,
        }
    )
    assets.write_json(attempt / "acquisition.json", receipt)
    attempt.rename(target)
    return target


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps({"directory": str(acquire()), "upstream_code_executed": False}))


if __name__ == "__main__":
    main()
