"""Real local Git objects and independent horizon examples, no upstream execution."""

import copy
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil
import pytest

from packages.mining import public_assets as assets
from packages.mining.research_dataset import CSV_FIELDS, PARTITIONS
from packages.semantic import artifacts
from packages.semantic import evaluation_population as population
from packages.semantic import evaluation_weights as weights
from packages.semantic.policy import SemanticError
from scripts.semantic_evaluation_audit import supervised
from tests.test_semantic_evaluation_weights import metadata_source


@pytest.fixture
def directory():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="evaluation-population-") as name:
        yield Path(name)


def source_fixture(directory, monkeypatch, *, gap=None):
    work = directory / "work"
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    git = shutil.which("git")
    assert git

    def run(*args, date=None):
        active = env | ({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {})
        return (
            subprocess.check_output(
                [git, "-C", str(work), *args],
                env=active,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            .decode()
            .strip()
        )

    run("init", "--initial-branch=fixture")
    run("config", "user.name", "Synthetic Source")
    run("config", "user.email", "synthetic@example.invalid")
    run("config", "core.hooksPath", str(directory / "no-hooks"))

    def commit(name, content, message, date):
        (work / name).write_bytes(content)
        run("add", "--", name)
        run("commit", "--quiet", "-m", message, date=date)
        return run("rev-parse", "HEAD")

    root = commit("outside.py", b"outside = 0\n", "initial", "2019-01-01T00:00:00+00:00")
    ids = {}
    for year in (2020, 2023, 2024, 2025):
        name = f"code{year}.py"
        ids[f"feature{year}"] = commit(
            name, b"value = 1\n", "implement feature", f"{year}-01-01T00:00:00+00:00"
        )
        ids[f"fix{year}"] = commit(
            name, b"value = 2\n", "fix error", f"{year}-01-10T00:00:00+00:00"
        )
        if year == 2023 and gap == "merge":
            tree, parent = run("rev-parse", "HEAD^{tree}"), run("rev-parse", "HEAD")
            merge = run(
                "commit-tree",
                tree,
                "-p",
                parent,
                "-p",
                root,
                "-m",
                "fix error",
                date="2023-02-01T00:00:00+00:00",
            )
            run("update-ref", "HEAD", merge)
            ids["gap"] = merge
        if year == 2024 and gap == "binary":
            commit("opaque.bin", b"\0before", "add payload", "2024-01-15T00:00:00+00:00")
            ids["gap"] = commit("opaque.bin", b"\0after", "fix error", "2024-02-01T00:00:00+00:00")
        if year == 2024 and gap == "filter":
            commit(
                "bad.py",
                b'broken = "unterminated\n',
                "add syntax sample",
                "2024-01-15T00:00:00+00:00",
            )
            ids["gap"] = commit("bad.py", b"valid = 1\n", "fix error", "2024-02-01T00:00:00+00:00")
        if year == 2024 and gap == "encoding":
            tree, parent = run("rev-parse", "HEAD^{tree}"), run("rev-parse", "HEAD")
            stamp = int(datetime(2024, 2, 1, tzinfo=UTC).timestamp())
            raw = (
                f"tree {tree}\nparent {parent}\n"
                f"author Fixture <synthetic@example.invalid> {stamp} +0000\n"
                f"committer Fixture <synthetic@example.invalid> {stamp} +0000\n\n"
            ).encode()
            result = subprocess.run(
                [git, "-C", str(work), "hash-object", "-t", "commit", "-w", "--stdin"],
                input=raw + b"fix error \xff\n",
                env=env,
                capture_output=True,
                timeout=10,
                check=True,
            )
            ids["gap"] = result.stdout.decode().strip()
            run("update-ref", "HEAD", ids["gap"])
    ids["outside_fix"] = commit(
        "outside.py", b"outside = 1\n", "fix error", "2025-02-01T00:00:00+00:00"
    )
    head = commit("final.txt", b"end\n", "maintenance", "2026-06-01T00:00:00+00:00")
    source = directory / "source"
    source.mkdir()
    run("clone", "--bare", "--no-hardlinks", str(work), str(source / "source.git"))
    assets.write_json(
        source / "acquisition.json",
        assets.sealed(
            {
                "repository": population.REPOSITORY,
                "head": head,
                "acquired_at": "2026-10-15T00:00:00+00:00",
                "synthetic": True,
            }
        ),
    )
    dataset = directory / "dataset"
    dataset.mkdir()
    for part in PARTITIONS:
        with (dataset / (part + ".csv")).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            if part == "test":
                writer.writerow(
                    {
                        "canonical_repository": population.REPOSITORY,
                        "commit_id": root,
                        "event_epoch": 0,
                        "label": "deliberately unused",
                    }
                )
    assets.write_json(
        dataset / "manifest.json",
        assets.sealed(
            {
                "files": {
                    part: artifacts.file_digest(dataset / (part + ".csv")) for part in PARTITIONS
                }
            }
        ),
    )
    reserve_root = directory / "reserved"
    reserve_root.mkdir()
    attempt = reserve_root / ".attempt"
    attempt.mkdir()
    assets.write_json(
        attempt / "reservation.json", {"records": [{"commit_id": ids["feature2025"]}]}
    )
    reservation = artifacts.publish(
        attempt, reserve_root, {"version": "unlabeled-future-reservation-v1"}, ["reservation.json"]
    )
    weight_source = metadata_source(directory, monkeypatch)
    evidence = weights.register(weight_source, directory / "weights")
    declaration = population.declare(
        source, dataset, reservation, evidence, directory / "declarations", head
    )
    return {
        "source": source,
        "dataset": dataset,
        "declaration": declaration,
        "ids": ids,
        "root": root,
    }


def observe(fixture, directory, name="full", **kwargs):
    return population.observe(
        fixture["declaration"],
        fixture["source"],
        directory / (name + "-cache"),
        directory / name,
        **kwargs,
    )


def read_rows(audit):
    return json.loads((audit / "outcomes.json").read_text())["records"]


def test_real_szz_roles_complete_coverage_and_outside_identity(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    audit = observe(fixture, directory)
    _, summary = population.verify_audit(audit)
    rows = {r["commit_id"]: r for r in read_rows(audit)}
    assert len(rows) == 9  # Fix commits themselves remain in the target population.
    for year, role in zip((2020, 2023, 2024, 2025), population.ROLES, strict=True):
        positive = rows[fixture["ids"][f"feature{year}"]]
        negative = rows[fixture["ids"][f"fix{year}"]]
        assert positive["provisional_label"] == 1 and positive["role"] == role
        assert negative["provisional_label"] == 0
        assert positive["positive_fix_ids"] == [fixture["ids"][f"fix{year}"]]
        assert positive["reconstructed_available_epoch"] == negative["committer_epoch"] + 7 * 86400
        assert negative["reconstructed_available_epoch"] == negative["committer_epoch"] + 97 * 86400
        assert positive["gold"] is False and positive["actual_label_available_at"] is None
    units = [json.loads(line) for line in (audit / "observations.jsonl").read_bytes().splitlines()]
    outside = next(r for r in units if r["metadata"]["commit_id"] == fixture["ids"]["outside_fix"])
    assert outside["outside_frame"] == [fixture["root"]]
    assert outside["trace"]["status"] == "partial"
    assert not outside["global_gaps"] and not outside["positive_targets"]
    link = next(r for r in units if r["metadata"]["commit_id"] == fixture["ids"]["fix2025"])[
        "trace"
    ]["links"][0]
    assert link["blamed_sha"] == fixture["ids"]["feature2025"]
    assert link["line_hash"] and link["old_blob"] and link["eligible"] is True
    assert summary["complete_population"] and not summary["calibration_positive_min10"]
    assert summary["research_training_ready"] is False


@pytest.mark.parametrize(
    "gap,year",
    [
        ("merge", 2023),
        ("binary", 2024),
        ("filter", 2024),
        ("encoding", 2024),
    ],
)
def test_real_unsupported_evidence_is_unknown_not_clean(directory, monkeypatch, gap, year):
    fixture = source_fixture(directory, monkeypatch, gap=gap)
    audit = observe(fixture, directory)
    rows = {r["commit_id"]: r for r in read_rows(audit)}
    assert rows[fixture["ids"][f"fix{year}"]]["provisional_label"] is None
    assert fixture["ids"]["gap"] in rows[fixture["ids"][f"fix{year}"]]["gap_fix_ids"]
    # Existing positive evidence is still provisional; no claim of the earliest real bug.
    assert rows[fixture["ids"][f"feature{year}"]]["provisional_label"] == 1
    assert rows[fixture["ids"]["feature2025"]]["provisional_label"] == 1
    population.verify_audit(audit)


def horizon_example(offset, *, positive=False, gap=False):
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    target = {"commit_id": "a" * 40, "committer_epoch": start, "role": population.ROLES[3]}
    event = {"commit_id": "b" * 40, "committer_epoch": start + offset}
    observation = {
        "metadata": event,
        "positive_targets": [target["commit_id"]] if positive else [],
        "target_gaps": [],
        "global_gaps": ["missing"] if gap else [],
    }
    return {"targets": [target], "observations": [event]}, [observation]


@pytest.mark.parametrize(
    "offset,positive,gap,label",
    [
        (90 * 86400, True, False, 1),
        (90 * 86400 + 1, True, False, 0),
        (90 * 86400, False, True, None),
        (90 * 86400 + 1, False, True, 0),
        (-1, True, True, 0),
        (1, True, True, 1),
    ],
)
def test_independent_inclusive_horizon_and_lag(offset, positive, gap, label):
    declaration, observations = horizon_example(offset, positive=positive, gap=gap)
    actual = population.outcomes(declaration, observations)[0]
    assert actual["provisional_label"] == label
    assert actual["actual_label_available_at"] is None and actual["gold"] is False


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "clock", "foreign"])
def test_horizon_requires_all_observation_identities(corruption):
    declaration, observations = horizon_example(10, positive=True)
    if corruption == "missing":
        observations.clear()
    elif corruption == "duplicate":
        observations *= 2
    elif corruption == "clock":
        observations[0]["metadata"] = observations[0]["metadata"] | {"committer_epoch": 1}
    else:
        observations[0]["positive_targets"] = ["c" * 40]
    with pytest.raises(SemanticError):
        population.outcomes(declaration, observations)


def test_interrupted_units_resume_and_empty_cache_reproduce_bytes(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    real_save = population.Context.save

    def interrupted(self, *args):
        result = real_save(self, *args)
        if self.created == 2:
            raise SemanticError("TEST_INTERRUPTION_AFTER_COMPLETED_UNIT")
        return result

    with monkeypatch.context() as active:
        active.setattr(population.Context, "save", interrupted)
        with pytest.raises(SemanticError, match="TEST_INTERRUPTION"):
            observe(fixture, directory, "resumable")
    assert len(list((directory / "resumable-cache").glob("*-*.json"))) == 2
    assert not list((directory / "resumable").glob("*/manifest.json"))
    resumed = observe(fixture, directory, "resumable")
    independent = observe(fixture, directory, "independent")
    _, summary = population.verify_audit(resumed)
    assert summary["prior_completed_units_reused"] >= 1
    for name in ("declaration.json", "observations.jsonl", "outcomes.json"):
        assert (resumed / name).read_bytes() == (independent / name).read_bytes()
    _, second = population.verify_audit(independent)
    operational = {"units_created", "prior_completed_units_reused", "cache_hits_total"}
    assert {k: v for k, v in summary.items() if k not in operational} == {
        k: v for k, v in second.items() if k not in operational
    }


def test_pilot_cannot_claim_population_and_tampering_refused(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    pilot = observe(fixture, directory, "pilot", pilot=2)
    _, summary = population.verify_audit(pilot)
    assert summary["completed_observations"] == 2 and not summary["complete_population"]
    assert read_rows(pilot) == []
    value = population.load_declaration(fixture["declaration"])
    value["targets"][0]["role"] = population.ROLES[3]
    with pytest.raises(SemanticError, match="EVAL_ROLE_DRIFT"):
        population.validate_declaration(
            assets.sealed({k: v for k, v in value.items() if k != "content_hash"})
        )
    cached = next((directory / "pilot-cache").glob("observation-*.json"))
    cached.write_bytes(cached.read_bytes() + b" ")
    with pytest.raises(SemanticError, match="EVAL_CACHE_DRIFT"):
        observe(fixture, directory, "pilot")
    assert cached.exists()


def test_source_drift_overlap_and_prewrite_capacity_refused(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    with pytest.raises(SemanticError, match="EVAL_OUTPUT_OVERLAP"):
        population.observe(
            fixture["declaration"],
            fixture["source"],
            fixture["source"] / "cache",
            directory / "out",
        )
    with monkeypatch.context() as active:
        active.setattr(population, "MAX_TREE", 1)
        with pytest.raises(SemanticError, match="EVAL_CACHE_LIMIT"):
            observe(fixture, directory)
    assert not list((directory / "full-cache").glob("observation-*.json"))
    config = fixture["source"] / "source.git/config"
    config.write_bytes(config.read_bytes() + b"\n# changed\n")
    with pytest.raises(SemanticError, match="EVAL_SOURCE_DRIFT"):
        observe(fixture, directory, "drift")


def test_supervisor_deadline_removes_owned_real_grandchild(directory):
    ready = directory / "owned-pids.json"
    code = (
        "import subprocess,sys,time,json,os; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid]));time.sleep(60)"
    )
    with pytest.raises(SemanticError, match="EVAL_PHASE_DEADLINE"):
        supervised(["-c", code, str(ready)], directory / "deadline.log", seconds=2)
    pids = json.loads(ready.read_text())
    stop = time.monotonic() + 5

    def alive(pid):
        try:
            return psutil.Process(pid).status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
        except psutil.NoSuchProcess:
            return False

    while any(alive(pid) for pid in pids) and time.monotonic() < stop:
        time.sleep(0.02)
    assert not any(alive(pid) for pid in pids)
    proof = json.loads((directory / "deadline.log.json").read_text())
    assert proof["completed"] is False and proof["reason"] == "EVAL_PHASE_DEADLINE"


def test_cli_rejects_direct_child_before_input_read(directory):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("DG_")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.semantic_evaluation_audit",
            "weights",
            "--inputs",
            str(directory / "absent"),
            "--output",
            str(directory / "out"),
            "--result",
            str(directory / "result.json"),
            "--log",
            str(directory / "log.txt"),
            "--child",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "EVAL_CHILD_PARENT_REQUIRED" in result.stdout
    assert not (directory / "out").exists()


@pytest.mark.parametrize("case", ["eligibility", "line", "positive", "clock", "target_parse"])
def test_line_decisions_are_checked_against_original_facts(case):
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    target = {"commit_id": "a" * 40, "committer_epoch": start, "role": population.ROLES[3]}
    fix = {"commit_id": "b" * 40, "parents": ["c" * 40], "committer_epoch": start + 86400}
    evidence = {
        "metadata": {k: v for k, v in target.items() if k != "role"},
        "parse_status": "parsed",
        "files": [],
    }
    link = {
        "blamed_sha": target["commit_id"],
        "fix_sha": fix["commit_id"],
        "parent_sha": fix["parents"][0],
        "eligible": True,
        "reason": None,
        "fixed_line": 1,
        "blamed_line": 1,
        "line_hash": "d" * 64,
        "label_available_at": datetime.fromtimestamp(start + 8 * 86400, UTC).isoformat(),
    }
    observation = {
        "metadata": fix,
        "trace": {"links": [link], "files": [], "status": "traced", "reasons": []},
        "positive_targets": [target["commit_id"]],
        "target_gaps": [],
        "outside_frame": [],
        "global_gaps": [],
        "target_evidence": {target["commit_id"]: evidence},
    }
    declaration = {"targets": [target], "reachable_metadata": {}}
    population.verify_observation(observation, declaration)
    bad = copy.deepcopy(observation)
    if case == "eligibility":
        bad["trace"]["links"][0]["eligible"] = False
    elif case == "line":
        bad["trace"]["links"][0]["fixed_line"] = 0
    elif case == "positive":
        bad["positive_targets"] = []
    elif case == "clock":
        bad["trace"]["links"][0]["label_available_at"] = "2025-01-01T00:00:00+00:00"
    else:
        bad["target_evidence"][target["commit_id"]]["parse_status"] = "unavailable"
    with pytest.raises(SemanticError):
        population.verify_observation(bad, declaration)


def test_source_final_drift_preserves_attempt_but_does_not_publish(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    original = population._inventory
    called = 0

    def drift(source, check):
        nonlocal called
        called += 1
        if called == 2:
            config = source / "source.git/config"
            config.write_bytes(config.read_bytes() + b"\n# injected final drift\n")
        return original(source, check)

    monkeypatch.setattr(population, "_inventory", drift)
    with pytest.raises(SemanticError, match="EVAL_FINAL_DRIFT"):
        observe(fixture, directory)
    assert list((directory / "full-cache").glob("observation-*.json"))
    assert list((directory / "full").glob(".observe-*"))
    assert not list((directory / "full").glob("*/manifest.json"))


def test_identity_exclusion_stays_cancellable_without_per_row_os_queries(directory, monkeypatch):
    dataset = directory / "exclusion"
    dataset.mkdir()
    for index, part in enumerate(PARTITIONS):
        with (dataset / (part + ".csv")).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in range(200):
                writer.writerow(
                    {
                        "canonical_repository": population.REPOSITORY,
                        "commit_id": f"{index * 200 + row:040x}",
                        "label": "unused",
                    }
                )
    assets.write_json(
        dataset / "manifest.json",
        assets.sealed(
            {
                "files": {
                    part: artifacts.file_digest(dataset / (part + ".csv")) for part in PARTITIONS
                }
            }
        ),
    )
    original = csv.DictReader
    consumed, checks = 0, 0

    class Counted:
        def __init__(self, stream):
            self.rows = original(stream)
            self.fieldnames = self.rows.fieldnames

        def __iter__(self):
            nonlocal consumed
            for row in self.rows:
                consumed += 1
                yield row

    monkeypatch.setattr(population.csv, "DictReader", Counted)

    def cancel():
        nonlocal checks
        checks += 1
        if consumed >= 128:
            raise SemanticError("TEST_EXCLUSION_CANCELLED")

    before = {p.name: p.read_bytes() for p in dataset.iterdir()}
    with pytest.raises(SemanticError, match="TEST_EXCLUSION_CANCELLED"):
        population._excluded(dataset, cancel)
    assert 128 <= consumed < 200 and checks < 10
    assert {p.name: p.read_bytes() for p in dataset.iterdir()} == before


def test_real_audit_does_not_walk_whole_cache_for_every_unit(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    cache = directory / "performance-cache"
    original = Path.iterdir
    walks = 0

    def counted(path):
        nonlocal walks
        if path == cache:
            walks += 1
        return original(path)

    monkeypatch.setattr(Path, "iterdir", counted)
    audit = observe(fixture, directory, "performance")
    _, summary = population.verify_audit(audit)
    assert summary["completed_observations"] == 9
    assert summary["units_created"] > 9
    assert walks <= 5


def test_final_reconciliation_refuses_external_cache_entry(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    original = population.Context.observation

    def interfere(context, item):
        value = original(context, item)
        (context.cache / "unexpected-external.txt").write_bytes(b"external")
        return value

    monkeypatch.setattr(population.Context, "observation", interfere)
    with pytest.raises(SemanticError, match="EVAL_CACHE_UNKNOWN"):
        observe(fixture, directory, pilot=1)
    assert (directory / "full-cache/unexpected-external.txt").read_bytes() == b"external"
    assert not list((directory / "full").glob("*/manifest.json"))


def test_temporary_publication_peak_checked_without_removing_crash_parts(directory, monkeypatch):
    fixture = source_fixture(directory, monkeypatch)
    cache = directory / "full-cache"
    cache.mkdir()
    part = cache / (".unit-part-" + "a" * 32)
    part.write_bytes(b"x" * 1536)
    monkeypatch.setattr(population, "MAX_TREE", 2048)
    with pytest.raises(SemanticError, match="EVAL_CACHE_LIMIT"):
        observe(fixture, directory)
    assert part.read_bytes() == b"x" * 1536
    assert not list(cache.glob("observation-*.json"))
