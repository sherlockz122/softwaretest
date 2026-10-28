"""Independent terminal-race counterexamples and bounded real Git probe contracts."""

import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from packages.repositories import parser
from packages.repositories.clone import ExecutionStopped
from packages.repositories.safety import RepositoryError


@pytest.mark.parametrize("scenario", ["stdout", "stderr", "deadline", "cancel"])
@pytest.mark.parametrize("allow", [False, True])
def test_terminal_poll_cannot_bypass_final_bounds(tmp_path, monkeypatch, scenario, allow):
    revoked = False
    reader = None

    def tick():
        if revoked:
            raise ExecutionStopped

    class TerminalChild:
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.streams = kwargs
            self.done = False

        def poll(self):
            nonlocal revoked
            if not self.done:
                self.done = True
                if scenario in {"stdout", "stderr"}:
                    stream = self.streams[scenario]
                    stream.write(b"x" * (2 if scenario == "stdout" else 65537))
                    stream.flush()
                elif scenario == "deadline":
                    reader.deadline = 0
                else:
                    revoked = True
            return 0

    monkeypatch.setattr(parser.subprocess, "Popen", TerminalChild)
    reader = parser.NativeReader(tmp_path, tmp_path, {}, tick, time.monotonic() + 30, "git")
    if scenario == "stdout" and allow:
        assert reader.command(["anything"], limit=1, allow_limit=True) is None
    else:
        error = ExecutionStopped if scenario == "cancel" else RepositoryError
        with pytest.raises(error):
            reader.command(["anything"], limit=1, allow_limit=allow)
    assert not (tmp_path / "parse.out").exists()
    assert not (tmp_path / "parse.err").exists()


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("status", [False, True])
def test_valid_terminal_output_and_exit_status_are_preserved(tmp_path, monkeypatch, code, status):
    calls = []

    class Child:
        returncode = code

        def __init__(self, *args, **kwargs):
            self.stream = kwargs["stdout"]
            self.done = False

        def poll(self):
            if not self.done:
                self.stream.write(b"ok\x00")
                self.stream.flush()
                self.done = True
            return code

    monkeypatch.setattr(parser.subprocess, "Popen", Child)
    reader = parser.NativeReader(
        tmp_path, tmp_path, {}, lambda: calls.append(1), time.monotonic() + 30, "git"
    )
    result = reader.command(["anything"], limit=3, codes=(0, 1), exit_code=status)
    assert result == (code if status else b"ok\x00")
    assert len(calls) >= 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "counts,repeats",
    [([], 3), ([True], 3), ([0], 3), ([101], 3), ([2, 1], 3), ([1, 1], 3), ([1], 2)],
)
def test_invalid_probe_policy_rejected(counts, repeats):
    from scripts.probe_native_read_baseline import policy

    with pytest.raises(ValueError, match="POLICY"):
        policy(counts, repeats)


def test_equivalence_does_not_accept_one_changed_digest():
    from scripts.probe_native_read_baseline import compare

    rows = [
        {
            "cached": bool(i % 2),
            "native_seconds": i + 1,
            "members": ["a" * 40],
            "member_digest": "a" * 64,
            **dict.fromkeys(
                ["native_digest", "input_digest", "numeric_digest", "causal_digest"], "ok"
            ),
        }
        for i in range(6)
    ]
    result = compare(rows)
    assert result["uncached"]["median_seconds"] == 3
    assert result["cached"]["range_seconds"] == [2, 6]
    rows[-1]["input_digest"] = "changed"
    with pytest.raises(ValueError, match="EQUIVALENCE_INPUT_DIGEST"):
        compare(rows)


@pytest.mark.parametrize("failure", ["deadline", "rss", "capacity"])
def test_probe_supervision_limits_remain_enforced(monkeypatch, failure):
    from scripts import probe_native_read_baseline as probe

    memory = SimpleNamespace(peak=3 * 1024**3 if failure == "rss" else 0)
    metric = probe.Measurement(0 if failure == "deadline" else time.monotonic() + 30, memory)
    monkeypatch.setattr(
        probe.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=0 if failure == "capacity" else 10 * 1024**3),
    )
    with pytest.raises(ValueError, match="NATIVE_MEASURE_" + failure.upper()):
        metric.tick()
    assert metric.phase()["ticks"] == 1 and metric.phase()["tick_seconds"] >= 0


def test_real_probe_binds_members_alternates_and_preserves_results():
    import json

    from packages.mining import public_assets as assets
    from scripts.probe_native_read_baseline import probe

    root = assets.ROOT / "runtime/acceptance"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="native-measure-test-") as work:
        output = Path(work) / "probe"
        result = probe(output, [3])
        rows = [json.loads(line) for line in (output / "trials.jsonl").read_text().splitlines()]
        assert [row["cached"] for row in rows] == [False, True, True, False, False, True]
        assert result["windows"][0]["summary"]["all_exact"]
        assert all(row["actual_commits"] == 3 and row["files"] == 3 for row in rows)
        assert all(row["phases"]["record"]["ticks"] > 0 for row in rows)
        assert all(row["cache"]["hits"] > 0 for row in rows if row["cached"])
        assert result["unmeasured"] and not result["candidate_release"]
        assert result["sampled_peak_rss_bytes"] > 0 and result["rss_samples"] > 0
        preserved = (output / "summary.json").read_bytes()
        with pytest.raises(ValueError, match="OUTPUT"):
            probe(output, [3])
        assert (output / "summary.json").read_bytes() == preserved


@pytest.mark.parametrize("unsafe", ["shallow", "info/grafts", "objects/info/alternates"])
def test_probe_source_rejects_unsafe_object_storage(unsafe):
    from packages.mining import public_assets as assets
    from scripts.probe_native_read_baseline import source_binding

    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="native-source-test-") as work:
        source = Path(work)
        assets.write_json(
            source / "acquisition.json",
            assets.sealed(
                {
                    "version": "semantic-git-acquisition-v1",
                    "repository": "inert",
                    "acquired_at": "2020-01-01T00:00:00+00:00",
                    "head": "a" * 40,
                }
            ),
        )
        path = source / "source.git" / unsafe
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unsafe")
        with pytest.raises(ValueError, match="STORAGE"):
            source_binding(source)
