"""Deterministic real Git history, with clocks deliberately different from DAG order."""

import os
from pathlib import Path

from tests.repository_support import git


def history(root, empty=False):
    root = Path(root)
    root.mkdir()
    git("init", "-b", "main", str(root))
    git("-C", str(root), "config", "user.name", "Fixture 作者")
    git("-C", str(root), "config", "user.email", " AUTHOR@Example.invalid ")
    git("-C", str(root), "config", "core.autocrlf", "false")
    counter = 0
    shas = {}

    def commit(label, message=None):
        nonlocal counter
        counter += 1
        env = dict(
            os.environ,
            GIT_AUTHOR_DATE=f"{1600000000 + counter} +0800",
            GIT_COMMITTER_DATE=f"{1600000000 + (30 - counter if counter > 9 else counter)} -0500",
        )
        git("-C", str(root), "add", "--all")
        git(
            "-C",
            str(root),
            "commit",
            "--allow-empty",
            "--allow-empty-message",
            "-m",
            label if message is None else message,
            env=env,
        )
        shas[label] = git("-C", str(root), "rev-parse", "HEAD").decode().strip()

    if empty:
        return root, shas
    (root / "old.txt").write_bytes(b"one\ntwo  \nno final newline")
    (root / "binary.bin").write_bytes(b"\0\1binary")
    (root / "大文件.txt").write_bytes(b"x" * (1024**2 + 1))
    (root / "diff.txt").write_bytes(b"abcdefghijklmnopqrstuvwxy\n" * 14000)
    commit("root")
    git("-C", str(root), "mv", "old.txt", "renamed.txt")
    commit("rename")
    (root / "renamed.txt").write_bytes(b"one\n++content  \nlast\n")
    (root / "unicode 文件.txt").write_text("内容\n", encoding="utf-8")
    commit("modify")
    (root / "renamed.txt").unlink()
    commit("delete")
    commit("empty-message", "")
    git("-C", str(root), "checkout", "-b", "side")
    (root / "side.txt").write_text("side\n", encoding="utf-8")
    commit("side")
    git("-C", str(root), "checkout", "main")
    (root / "main.txt").write_text("main\n", encoding="utf-8")
    commit("main")
    env = dict(
        os.environ, GIT_AUTHOR_DATE="1600000008 +0800", GIT_COMMITTER_DATE="1600000008 -0500"
    )
    git("-C", str(root), "merge", "--no-ff", "side", "-m", "merge", env=env)
    shas["merge"] = git("-C", str(root), "rev-parse", "HEAD").decode().strip()
    counter = 8
    for index in range(6):
        (root / "main.txt").write_text(f"iteration {index}\n", encoding="utf-8")
        commit("iteration-" + str(index))
    return root, shas
