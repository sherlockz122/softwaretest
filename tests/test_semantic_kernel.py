"""Independent inert Git/metric/resource oracles; fixtures never prove predictive quality."""

import csv
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import psutil
import pytest

from packages.mining import public_assets as assets
from packages.mining.research_dataset import CSV_FIELDS, NUMERIC_FIELDS, PARTITIONS
from packages.semantic import artifacts, control
from packages.semantic.encoder import windows
from packages.semantic.git_inputs import changed_lines, extract, native_environment, text
from packages.semantic.policy import SemanticError
from packages.semantic.study import fitted_hash, gain_gate, partitions, study, train_round


@pytest.fixture
def work():
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="semantic-unit-", dir=parent) as name:
        yield Path(name)


def git_fixture(root):
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    environment = native_environment(home, work)

    def git(*args):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + str(home), "-C", str(work), *args],
            env=environment,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, "inert local Git fixture command failed"
        return result.stdout.decode().strip()

    git("init", "--template=" + str(home))
    (work / "LICENSE.txt").write_bytes(b"Apache License\nlegacy notice \xa4\n")
    (work / "Main.java").write_bytes(b"class A { int x=0; }\n")
    git("add", "LICENSE.txt", "Main.java")
    git("update-index", "--chmod=+x", "LICENSE.txt")
    rows = []
    for i in range(13):
        environment.update(
            GIT_AUTHOR_DATE=f"{1000000000 + i * 1000} +0000",
            GIT_COMMITTER_DATE=f"{1000000000 + i * 1000} +0000",
        )
        (work / "Main.java").write_bytes(f"class A {{ int x={i}; }}\n".encode())
        git("add", "Main.java")
        git(
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-m",
            f"inert change {i}",
        )
        if i:
            rows.append(
                {
                    "canonical_repository": "apache/activemq",
                    "commit_id": git("rev-parse", "HEAD"),
                    "event_epoch": str(1000000000 + i * 1000),
                    "label": str(i % 2),
                    **{k: str(i + 1) for k in NUMERIC_FIELDS},
                }
            )
    source = root / "source"
    source.mkdir()
    bare = source / "source.git"
    subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=" + str(work),
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--bare",
            "--no-hardlinks",
            str(work),
            str(bare),
        ],
        env=environment,
        check=True,
        capture_output=True,
        timeout=20,
    )
    assets.write_json(
        source / "acquisition.json",
        assets.sealed(
            {
                "repository": "apache/activemq",
                "head": rows[-1]["commit_id"],
                "acquired_at": "2026-10-15T00:00:00+00:00",
            }
        ),
    )
    dataset = root / "dataset"
    dataset.mkdir()
    files = {}
    for index, part in enumerate(PARTITIONS):
        path = dataset / (part + ".csv")
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows[index * 4 : (index + 1) * 4])
        files[part] = artifacts.file_digest(path)
    assets.write_json(
        dataset / "manifest.json", assets.sealed({"files": files, "fixture_not_real_effect": True})
    )
    return dataset, source, rows


def test_native_real_roles_clock_license_and_immutable_reload(work):
    dataset, source, original = git_fixture(work)
    output = extract(dataset, source, work / "outputs")
    manifest = artifacts.verify(output)
    assert manifest["accepted"] == 12 and manifest["reasons"] == {}
    records = list(artifacts.records(output))
    assert records[0]["roles"]["added"] == "class A { int x=1; }\n"
    assert records[0]["roles"]["deleted"] == "class A { int x=0; }\n"
    assert records[0]["roles"]["base"] == records[0]["roles"]["deleted"]
    assert records[0]["member"]["event_epoch"] == original[0]["event_epoch"]
    assert all(set(manifest["supports"][p]) == {"0", "1"} for p in PARTITIONS)
    assert len(json.loads((output / "licenses.json").read_text())["documents"]) == 1
    assert records[0]["root_license_refs"][0]["path"] == "LICENSE.txt"
    (output / "records.jsonl").write_bytes((output / "records.jsonl").read_bytes() + b"\n")
    with pytest.raises(SemanticError, match="ARTIFACT_DRIFT"):
        artifacts.verify(output)


def test_frozen_csv_drift_rejected_before_git(work):
    dataset, source, _ = git_fixture(work)
    with (dataset / "train.csv").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(SemanticError, match="DATASET_DRIFT"):
        extract(dataset, source, work / "outputs")


def test_git_environment_fails_before_missing_object_audit(work, monkeypatch):
    dataset, source, _ = git_fixture(work)
    monkeypatch.setattr(
        "packages.semantic.git_inputs.NativeReader.command", lambda *a, **k: b"false"
    )
    with pytest.raises(SemanticError, match="GIT_STORAGE_INVALID"):
        extract(dataset, source, work / "outputs")
    assert not list((work / "outputs").glob("*/manifest.json"))


@pytest.mark.parametrize(
    "body,code",
    [(b"x\0y", "BINARY"), (b"\xff", "ENCODING"), (b"a\n" * 5001, "LINE_LIMIT")],
    ids=["binary", "invalid-utf8", "line-limit"],
)
def test_text_hard_boundaries(body, code):
    with pytest.raises(SemanticError, match=code):
        text(body)


def test_changed_lines_preserve_diff_polarity_and_header_like_source():
    added, deleted = changed_lines("a\n+++ literal\nlast\n", "a\n--- literal\nlast\n")
    assert added == "--- literal\n" and deleted == "+++ literal\n"


def test_changed_lines_preserve_original_crlf():
    assert changed_lines("a\r\nold\r\n", "a\r\nnew\r\n") == ("new\r\n", "old\r\n")


def test_token_window_endpoints_and_exact_coverage():
    chunks, audit = windows(list(range(300)))
    assert chunks == [list(range(126)), list(range(252, 300))]
    assert audit["covered_tokens"] == 174 and audit["omitted_tokens"] == 126
    assert windows([])[1]["empty"] and windows([])[0] == []


@pytest.mark.parametrize("maximum,count", [(True, 2), (2, 2), (513, 2), (128, 0), (128, 9)])
def test_window_resource_policy(maximum, count):
    with pytest.raises(SemanticError, match="WINDOW_POLICY"):
        windows([1], maximum, count)


def test_native_environment_exact_trust_and_no_network(work, monkeypatch):
    monkeypatch.setenv("DG_MYSQL_PASSWORD", "fixture-secret")
    env = native_environment(work, work / "source.git")
    config = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert (
        config["safe.directory"] == str(work / "source.git") and config["protocol.allow"] == "never"
    )
    assert env["GIT_NO_LAZY_FETCH"] == "1" and "DG_MYSQL_PASSWORD" not in env
    with pytest.raises(assets.AssetError):
        native_environment(work, assets.ROOT.parent)


@pytest.mark.parametrize(
    "seconds,rss,expected", [(0.1, 6 * 1024**3, "DEADLINE"), (5, 1, "MEMORY_LIMIT")]
)
def test_supervisor_stops_child_and_preserves_failure(work, seconds, rss, expected):
    log = work / "child.log"
    pids = []
    with pytest.raises(SemanticError, match=expected):
        control.supervise(
            ["-c", "import time;time.sleep(20)"],
            log,
            seconds=seconds,
            rss_limit=rss,
            observe=lambda p: pids.append(p.pid),
        )
    assert not json.loads(log.with_suffix(".log.json").read_text())["completed"]
    assert all(not psutil.pid_exists(pid) for pid in pids)


def test_supervisor_cancellation_and_fast_output_limit(work):
    pids = []

    def cancel(p):
        pids.append(p.pid)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        control.supervise(["-c", "import time;time.sleep(20)"], work / "cancel.log", observe=cancel)
    assert all(not psutil.pid_exists(pid) for pid in pids)
    with pytest.raises(SemanticError, match="LOG_LIMIT"):
        control.supervise(["-c", "print('x'*(9*1024**2))"], work / "output.log")


def test_manifest_identity_and_path_fences(work):
    root = work / "objects"
    root.mkdir()
    attempt = root / ".stage"
    attempt.mkdir()
    (attempt / "value").write_bytes(b"123")
    published = artifacts.publish(attempt, root, {"version": "inert"}, ["value"])
    manifest = json.loads((published / "manifest.json").read_text())
    manifest["version"] = "different"
    manifest.pop("content_hash")
    (published / "manifest.json").write_text(json.dumps(assets.sealed(manifest)))
    with pytest.raises(SemanticError, match="MANIFEST_ID_DRIFT"):
        artifacts.verify(published)
    with pytest.raises(SemanticError, match="FILE_LIMIT"):
        artifacts.file_digest(published / "value", maximum=10**1000)


def test_predeclared_gain_gate_rejects_cost_regression_and_undefined():
    baseline = {"pr_auc": 0.2, "mcc": 0.1, "brier": 0.1, "effort": {"pofb20": 0.3}}
    improved = {"pr_auc": 0.25, "mcc": 0.2, "brier": 0.09, "effort": {"pofb20": 0.3}}
    assert gain_gate(baseline, improved, 42)["passed"]
    assert not gain_gate(baseline, improved | {"effort": {"pofb20": 0.2}}, 42)["passed"]
    assert (
        gain_gate(baseline, improved | {"effort": {"pofb20": None}}, 42)["reason"]
        == "undefined_metric"
    )


@pytest.mark.parametrize("kind", ["numeric_lr", "numeric_rf", "semantic_lr", "fusion_lr"])
def test_test_changes_cannot_affect_train_scaler_calibration_or_threshold(kind):
    rng = np.random.default_rng(3)
    rows = []
    for pi, part in enumerate(PARTITIONS):
        for i in range(24):
            rows.append(
                {
                    "member": {
                        "partition": part,
                        "canonical_repository": "fixture/repo",
                        "commit_id": f"{pi * 24 + i:040x}",
                        "event_epoch": str(pi * 100 + i),
                        "label": str(i % 2),
                        **{k: str(rng.uniform(0.1, 2)) for k in NUMERIC_FIELDS},
                    }
                }
            )
    x = rng.normal(size=(len(rows), 3076))
    parts = partitions(rows, x, kind)
    a, _, _ = train_round(parts, kind, 42)
    parts["test"]["x"][:] = 1e9
    parts["test"]["y"][:] = 0
    b, _, _ = train_round(parts, kind, 42)
    assert fitted_hash(a) == fitted_hash(b) and a["threshold"] == b["threshold"]


def test_complete_study_reload_repeat_and_population_binding(work):
    """Exercise publication and all comparators with inert, explicitly non-research data."""
    rng = np.random.default_rng(19)
    rows = []
    for pi, part in enumerate(PARTITIONS):
        for i in range(24):
            rows.append(
                {
                    "member": {
                        "partition": part,
                        "canonical_repository": "fixture/repo",
                        "commit_id": f"{pi * 24 + i:040x}",
                        "event_epoch": str(pi * 100 + i),
                        "label": str(i % 2),
                        **{k: str(rng.uniform(0.1, 2)) for k in NUMERIC_FIELDS},
                    },
                    "text_hash": f"{pi * 24 + i:064x}",
                    "files": [{"language_hint": "Java"}],
                }
            )
    root = work / "objects"
    root.mkdir()
    stage = root / ".inputs"
    stage.mkdir()
    (stage / "records.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    inputs = artifacts.publish(stage, root, {"accepted": 72}, ["records.jsonl"])
    stage = root / ".cache"
    stage.mkdir()
    np.save(stage / "embeddings.npy", rng.normal(size=(72, 3076)).astype(np.float32))
    assets.write_json(
        stage / "rows.json",
        {"records": [{"member": r["member"], "text_hash": r["text_hash"]} for r in rows]},
    )
    cache = artifacts.publish(
        stage,
        root,
        {"binding": {"inputs": artifacts.verify(inputs)["content_hash"]}, "shape": [72, 3076]},
        ["embeddings.npy", "rows.json"],
    )
    target = study(inputs, cache, work / "studies")
    manifest = artifacts.verify(target)
    report = assets.read_json(target / "report.json", lambda: None)
    assert manifest["runs"] == 12 and manifest["fitted_repeat_exact"]
    assert not report["candidate_release"] and not report["models_selected_using_test"]
    assert len([r for r in report["runs"] if r["repeated"]]) == 4
    assert len(manifest["files"]) == 25
    (cache / "embeddings.npy").write_bytes(b"damaged")
    with pytest.raises(SemanticError, match="ARTIFACT_DRIFT"):
        study(inputs, cache, work / "refused")
    assert not (work / "refused").exists()
