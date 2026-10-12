"""Independent native equality and cache cancellation/eviction/mode boundaries."""

import time
from types import SimpleNamespace

import pytest

from packages.mining.feature_reads import (
    CACHE_BYTES,
    CACHE_ENTRIES,
    cached_blobs,
    file_projections,
)
from packages.repositories.clone import ExecutionStopped
from packages.repositories.parse_policy import BLOB_LIMIT
from packages.repositories.safety import RepositoryError
from scripts.probe_feature_reads import CountedReader, environment, fixture
from tests.repository_support import git


def reader(value=b"body\n"):
    calls = []

    def blob(sha, mode):
        calls.append((sha, mode))
        return b"" if set(sha) == {"0"} or mode == "160000" else value

    return SimpleNamespace(blob=blob, tick=lambda: None, deadline=time.monotonic() + 60), calls


@pytest.mark.parametrize("value", [b"body\n", b"", None])
def test_same_object_reuse_restores_original_and_clears(value):
    source, calls = reader(value)
    original = source.blob
    with cached_blobs(source) as cache:
        assert source.blob("a" * 40, "100644") == value
        assert source.blob("a" * 40, "100755") == value
        assert len(calls) == 1 and cache.hits == 1
    assert source.blob is original and cache.bytes == 0 and not cache.entries


@pytest.mark.parametrize("stop", ["cancel", "capacity", "deadline"])
def test_supervised_cache_hit_can_interrupt_and_always_cleanup(stop):
    source, calls = reader()
    original = source.blob
    with pytest.raises((ExecutionStopped, RepositoryError)):
        with cached_blobs(source) as cache:
            source.blob("a" * 40, "100644")
            if stop == "deadline":
                source.deadline = time.monotonic() - 1
            else:

                def interrupt():
                    if stop == "cancel":
                        raise ExecutionStopped
                    raise RepositoryError(507, "REPOSITORY_CAPACITY_LOW")

                source.tick = interrupt
            source.blob("a" * 40, "100644")
    assert len(calls) == 1 and source.blob is original
    assert cache.bytes == 0 and not cache.entries


def test_null_and_gitlink_modes_never_reuse_regular_bytes():
    source, calls = reader()
    with cached_blobs(source) as cache:
        assert source.blob("a" * 40, "100644") == b"body\n"
        assert source.blob("a" * 40, "160000") == b""
        assert source.blob("0" * 40, "000000") == b""
        assert source.blob("a" * 40, "120000") == b"body\n"
        assert cache.hits == 1 and len(calls) == 3


@pytest.mark.parametrize("value", [None, b"x" * BLOB_LIMIT], ids=["oversize-sentinel", "one-MiB"])
def test_payload_and_entry_eviction_are_bounded(value):
    source, _ = reader(value)
    with cached_blobs(source) as cache:
        for index in range(CACHE_ENTRIES + 3):
            source.blob(f"{index + 1:040x}", "100644")
            assert cache.bytes <= CACHE_BYTES and len(cache.entries) <= CACHE_ENTRIES
        assert "0" * 39 + "1" not in cache.entries
        assert cache.peak_entries <= CACHE_ENTRIES and cache.peak_bytes <= CACHE_BYTES


def test_reader_cache_is_not_shared_between_attempts():
    first, a = reader(b"first")
    second, b = reader(b"second")
    with cached_blobs(first), cached_blobs(second):
        assert first.blob("a" * 40, "100644") == b"first"
        assert second.blob("a" * 40, "100644") == b"second"
    assert len(a) == len(b) == 1


@pytest.mark.parametrize("count", [0, 1, 100, 101, 1000])
def test_projection_groups_bound_queries_and_exclude_patch_payloads(count):
    queries = []

    def execute(query):
        compiled = query.compile()
        queries.append(compiled)
        assert "diff_text" not in str(compiled) and "line_numbers" not in str(compiled)
        ids = next(value for value in compiled.params.values() if isinstance(value, list))
        assert len(ids) <= 100
        return [SimpleNamespace(commit_id=key, ordinal=i) for key in ids for i in range(2)]

    commits = [SimpleNamespace(id=str(i)) for i in range(count)]
    groups = file_projections(SimpleNamespace(execute=execute), commits, lambda: None)
    assert len(queries) == (count + 99) // 100
    assert set(groups) == {row.id for row in commits}
    assert all([row.ordinal for row in group] == [0, 1] for group in groups.values())


def test_projection_limit_rejects_before_returning_corrupt_population():
    def execute(query):
        assert query.compile().params["param_1"] == 10001
        return (SimpleNamespace(commit_id="a", ordinal=i) for i in range(10001))

    with pytest.raises(RepositoryError, match="FEATURE_RESOURCE_LIMIT"):
        file_projections(SimpleNamespace(execute=execute), [SimpleNamespace(id="a")], lambda: None)


@pytest.mark.parametrize("kind", ["revert", "binary", "symlink", "large", "gitlink"])
def test_real_git_complete_records_equal_uncached(tmp_path, kind):
    env = environment(tmp_path / "home")
    bare = tmp_path / "source.git"
    shas = fixture(bare, env, 5)
    data = {
        "revert": b"value=0\n",
        "binary": b"\x00binary\n",
        "symlink": b"target.py",
        "large": b"x" * (BLOB_LIMIT + 1),
    }
    if kind == "gitlink":
        blob, mode = shas[-1], "160000"
    else:
        blob = (
            git("-C", str(bare), "hash-object", "-w", "--stdin", data=data[kind], env=env)
            .decode()
            .strip()
        )
        mode = "120000" if kind == "symlink" else "100644"
    tree = (
        git(
            "-C",
            str(bare),
            "mktree",
            data=f"{mode} blob {blob}\tother.py\n".replace("160000 blob", "160000 commit").encode(),
            env=env,
        )
        .decode()
        .strip()
    )
    shas.append(
        git("-C", str(bare), "commit-tree", tree, "-p", shas[-1], data=b"Fix edge\n", env=env)
        .decode()
        .strip()
    )
    attempts = [tmp_path / "a", tmp_path / "b"]
    for attempt in attempts:
        attempt.mkdir()
    readers = [CountedReader(bare, p, env, lambda: None, time.monotonic() + 120) for p in attempts]
    baseline = [readers[0].record(sha) for sha in shas]
    with cached_blobs(readers[1]):
        optimized = [readers[1].record(sha) for sha in shas]
    assert baseline == optimized
    assert readers[1].counts["cat-file blob"] < readers[0].counts["cat-file blob"]
    assert "blob" not in vars(readers[1])
    assert not any(p.exists() for a in attempts for p in (a / "parse.out", a / "parse.err"))
