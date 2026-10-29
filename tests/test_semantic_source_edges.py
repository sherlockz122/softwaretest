"""Independent real-Git edge contracts outside the fixed measurement safety suite."""

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil
import pytest

from packages.mining import public_assets as assets
from packages.semantic import source_resolution as source
from packages.semantic.git_inputs import native_environment


@pytest.fixture
def graph(monkeypatch):
    parent = assets.safe_path(assets.ROOT / "runtime/temp")
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="source-edges-") as temporary:
        directory = assets.safe_path(Path(temporary))
        bare = directory / "source.git"
        git = shutil.which("git")
        assert git
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
        )

        def command(*arguments, raw=None):
            return subprocess.run(
                [git, "--git-dir=" + str(bare), *arguments],
                input=raw,
                env=environment,
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout

        subprocess.run(
            [git, "init", "--quiet", "--bare", str(bare)],
            env=environment,
            capture_output=True,
            check=True,
            timeout=10,
        )

        def object_id(*arguments, raw=None):
            return command(*arguments, raw=raw).strip().decode("ascii")

        def tree(files):
            rows = []
            for name, content in sorted(files.items()):
                blob = object_id("hash-object", "-w", "--stdin", raw=content)
                rows.append(f"100644 blob {blob}\t{name}\n".encode())
            return object_id("mktree", raw=b"".join(rows))

        commits = {}
        invocations = []
        run = source.native_runtime.run

        def independently_observed_run(arguments, **options):
            identities = {}
            previous = options.get("observe")
            succeeded = False

            def observe(pid, created, descendants):
                identities.update({pid: created} | descendants)
                if previous is not None:
                    previous(pid, created, descendants)

            try:
                result = run(arguments, **(options | {"observe": observe}))
                assert result.cleanup_complete
                succeeded = True
                return result
            finally:
                assert identities
                for pid, created in identities.items():
                    try:
                        process = psutil.Process(pid)
                        assert process.create_time() != created or (
                            os.name != "nt" and process.status() == psutil.STATUS_ZOMBIE
                        )
                    except psutil.NoSuchProcess:
                        pass
                invocations.append(succeeded)

        monkeypatch.setattr(source.native_runtime, "run", independently_observed_run)

        def commit(files, parents, message, *, epoch=1672704000, encoding=None):
            headers = [
                "tree " + tree(files),
                *["parent " + sha for sha in parents],
                f"author Contract Fixture <fixture@example.invalid> {epoch} +0000",
                f"committer Contract Fixture <fixture@example.invalid> {epoch} +0000",
            ]
            if encoding:
                headers.append("encoding " + encoding)
            raw = "\n".join(headers).encode() + b"\n\n" + message
            sha = object_id("hash-object", "-t", "commit", "-w", "--stdin", raw=raw)
            metadata, _ = source.decode_commit(raw, sha)
            commits[sha] = metadata
            return sha

        code = b"".join(f"value_{i} = {i}\n".encode() for i in range(10))
        changed = code.replace(b"value_0 = 0", b"value_0 = 99")
        foundation = commit({}, [], b"foundation", epoch=1672531200)
        base = {"code.py": code, "README.md": b"old\n"}
        root = commit(base, [foundation], b"introduce code", epoch=1672617600)

        def reader(label):
            work = directory / label
            work.mkdir()
            return source.Reader(
                bare,
                work,
                native_environment(work, bare),
                lambda: None,
                time.monotonic() + 30,
                source_id="b" * 64,
                cache=True,
            )

        def observe(sha, target=root):
            current = reader("native")
            declaration = {
                "targets": [commits[target] | {"role": "calibration_candidate"}],
                "observations": [commits[sha]],
                "reachable_metadata": dict(commits),
            }
            cache = directory / "cache"
            cache.mkdir()
            execution = {"synthetic_integration_fixture": True}
            context = source.Context(
                declaration,
                directory,
                cache,
                current,
                lambda: None,
                binding="c" * 64,
                execution=execution,
                execution_check=lambda: execution,
            )
            value = context.observation(commits[sha])
            source.verify_unit(value, declaration)
            assert current.statistics["command_attempts"] == len(invocations)
            # This counter records successful returned Result objects only. The
            # independent PID checks above also cover expected nonzero Git exits.
            assert current.statistics["cleanup_confirmed"] == sum(invocations)
            return value

        yield {
            "commit": commit,
            "observe": observe,
            "reader": reader,
            "foundation": foundation,
            "root": root,
            "base": base,
            "changed": changed,
        }


def test_mixed_parent_union_does_not_hide_code_behind_docs(graph):
    commit, base, root = graph["commit"], graph["base"], graph["root"]
    first = commit(base | {"code.py": graph["changed"]}, [root], b"branch code")
    second = commit(base | {"README.md": b"new\n"}, [root], b"branch docs")
    merged = commit(
        base | {"code.py": graph["changed"], "README.md": b"new\n"},
        [first, second],
        b"fix error mixed merge",
    )
    value = graph["observe"](merged)
    result = value["resolution"]
    assert value["assessment"]["rule_candidate"]
    assert result["target_gaps"] == [root]
    assert not result["positive_targets"] and not result["global_gaps"]
    assert any(f["old_path"] == "README.md" for f in result["edges"][0]["files"])
    assert any(f["old_path"] == "code.py" for f in result["edges"][1]["files"])


def test_complete_docs_only_union_preserves_genuine_exclusion(graph):
    commit, base, root = graph["commit"], graph["base"], graph["root"]
    first = commit(base | {"README.md": b"first\n"}, [root], b"branch docs one")
    second = commit(base | {"README.md": b"second\n"}, [root], b"branch docs two")
    merged = commit(base | {"README.md": b"merged\n"}, [first, second], b"fix error docs")
    value = graph["observe"](merged)
    assert not value["assessment"]["rule_candidate"]
    assert any(e["value"] == "documentation_only" for e in value["assessment"]["evidence"])
    assert not value["resolution"]["global_gaps"]
    assert not value["resolution"]["target_gaps"]


def test_actual_missing_parent_object_cannot_infer_docs_only_union(graph):
    sha = graph["commit"](
        graph["base"] | {"README.md": b"missing-edge\n"},
        [graph["root"], "f" * 40],
        b"fix error unread parent",
    )
    value = graph["observe"](sha)
    assert value["assessment"]["rule_candidate"]
    assert value["resolution"]["global_gaps"]
    assert value["resolution"]["edges"][1]["read_failure"] == "REPOSITORY_PARSE_FAILED"
    assert not value["resolution"]["positive_targets"]


def test_actual_rename_retains_original_path_line_and_origin(graph):
    sha = graph["commit"](
        {"renamed.py": graph["changed"], "README.md": b"old\n"},
        [graph["root"]],
        b"fix error renamed file",
    )
    value = graph["observe"](sha)
    edge = value["resolution"]["edges"][0]
    assert any(
        f["change_type"] == "R" and f["old_path"] == "code.py" and f["new_path"] == "renamed.py"
        for f in edge["files"]
    )
    assert value["resolution"]["positive_targets"] == [graph["root"]]
    assert any(
        link["path"] == "code.py"
        and link["blamed_sha"] == graph["root"]
        and link["line_hash"] == hashlib.sha256(b"value_0 = 0").hexdigest()
        for link in edge["observation"]["trace"]["links"]
    )


def test_actual_unsupported_commit_encoding_remains_global_gap(graph):
    sha = graph["commit"](
        graph["base"] | {"code.py": graph["changed"]},
        [graph["root"]],
        b"fix error undecodable",
        encoding="fixture-invalid-codec",
    )
    value = graph["observe"](sha)
    assert value["resolution"]["global_gaps"] == ["message_encoding"]
    assert not value["resolution"]["positive_targets"]


@pytest.mark.parametrize(
    ("content", "reason"), [(b"\xff\xfe\n", "encoding"), (b"'''\nunclosed\n", "filter_unavailable")]
)
def test_actual_blob_or_filter_unavailability_cannot_become_clean(graph, content, reason):
    root = graph["commit"](
        graph["base"] | {"code.py": content},
        [graph["foundation"]],
        b"introduce unavailable content",
        epoch=1672617600,
    )
    sha = graph["commit"](
        graph["base"] | {"code.py": graph["changed"]}, [root], b"fix error unavailable content"
    )
    value = graph["observe"](sha, root)
    assert value["resolution"]["global_gaps"] == [reason]
    assert not value["resolution"]["positive_targets"]


def test_actual_parent_clock_inversion_remains_target_unknown(graph):
    root = graph["commit"](
        graph["base"], [graph["foundation"]], b"introduce future time", epoch=1672876800
    )
    sha = graph["commit"](
        graph["base"] | {"code.py": graph["changed"]},
        [root],
        b"fix error clock inversion",
        epoch=1672704000,
    )
    value = graph["observe"](sha, root)
    assert value["resolution"]["target_gaps"] == [root]
    assert not value["resolution"]["positive_targets"]
    assert value["resolution"]["edges"][0]["observation"]["trace"]["links"][0]["reason"] == (
        "event_time_inversion"
    )


def test_real_unrelated_and_reverse_ancestry_are_not_accepted(graph):
    sibling = graph["commit"](graph["base"], [graph["foundation"]], b"unrelated branch")
    child = graph["commit"](
        graph["base"] | {"code.py": graph["changed"]}, [graph["root"]], b"fix error child"
    )
    reader = graph["reader"]("ancestry")
    assert reader.ancestor(graph["root"], child)
    assert not reader.ancestor(child, graph["root"])
    assert not reader.ancestor(sibling, graph["root"])
    assert reader.statistics["command_attempts"] == reader.statistics["cleanup_confirmed"]
