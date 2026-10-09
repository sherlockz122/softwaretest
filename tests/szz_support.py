"""Small deterministic Git fixtures for line-level SZZ assertions."""

import os
import time
from datetime import UTC, datetime

from packages.repositories.parser import NativeReader
from tests.repository_support import git


def szz_history(root, scenario="modify", count=0, empty=False):
    root.mkdir()
    git("init", "-b", "main", str(root))
    git("-C", str(root), "config", "user.name", "SZZ Fixture")
    git("-C", str(root), "config", "user.email", "szz@example.invalid")
    git("-C", str(root), "config", "core.autocrlf", "false")
    shas = []
    if empty:
        return root, None, None, shas

    def commit(message):
        stamp = 1600000000 + len(shas)
        env = dict(
            os.environ, GIT_AUTHOR_DATE=f"{stamp} +0000", GIT_COMMITTER_DATE=f"{stamp} +0000"
        )
        git("-C", str(root), "add", "--all")
        git("-C", str(root), "commit", "--allow-empty", "-m", message, env=env)
        sha = git("-C", str(root), "rev-parse", "HEAD").decode().strip()
        shas.append(sha)
        return sha

    path = root / "code.py"
    old = "value = 0\n# old comment\n\n"
    if scenario == "string":
        old = 'text = """first\n# not a comment\nlast"""\n'
    if scenario == "string_blank":
        old = 'text = """first\n  \nlast"""\n'
    if scenario == "binary":
        path.write_bytes(b"\0old\n")
    elif scenario == "encoding":
        path.write_bytes(b"value = '\xff'\n")
    else:
        path.write_text(old, encoding="utf-8")
    base = commit("Initial code")
    if scenario == "parent":
        path.write_text("value = 1\n", encoding="utf-8")
        base = commit("Introduce regression")
    if scenario == "rename":
        git("-C", str(root), "mv", "code.py", "重命名 code.py")
        path = root / "重命名 code.py"
        commit("Rename file")
    if scenario == "delete":
        path.unlink()
    elif scenario == "add":
        (root / "new.py").write_text("new = 1\n", encoding="utf-8")
    elif scenario == "comment":
        path.write_text(old.replace("old comment", "new comment"), encoding="utf-8")
    elif scenario == "blank":
        path.write_text(old.replace("\n\n", "\n"), encoding="utf-8")
    elif scenario == "string":
        path.write_text(old.replace("not a comment", "new string"), encoding="utf-8")
    elif scenario == "string_blank":
        path.write_text(old.replace("\n  \n", "\n    \n"), encoding="utf-8")
    elif scenario == "binary":
        path.write_bytes(b"\0new\n")
    elif scenario == "encoding":
        path.write_bytes(b"value = '\xfe'\n")
    else:
        path.write_text("value = 2\n# new comment\n\n", encoding="utf-8")
    fixed = commit("Fix regression")
    for index in range(count):
        path.write_text(f"value = {3 + index}\n", encoding="utf-8")
        commit("Fix runtime regression")
    return root, base, fixed, shas


def reader_for(bare, attempt):
    attempt.mkdir()
    environment = dict(
        os.environ,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_CONFIG_COUNT="0",
    )
    return NativeReader(bare, attempt, environment, lambda: None, time.monotonic() + 30)


def event(index=0):
    return datetime.fromtimestamp(1600000000 + index, UTC).replace(tzinfo=None)
