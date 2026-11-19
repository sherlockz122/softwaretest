"""纵向接入：仓库轮次 → 数据集 → 训练。

- 内核级：审计结论 → 成员行转换（含缺失特征排除与缺失原因）
- 应用级（真实 MySQL）：观察关系落库、同绑定幂等、构建/训练全链路
  （prepare_repository 用合成审计产物替身，管线本身全真实）、
  审计不足与覆盖不完整的明确拒绝、自然数据不足的拒绝（真实四步管线夹具）
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.dataset_service import DatasetService
from packages.mining.feature_service import FeatureService
from packages.mining.fix import FixService
from packages.mining.source_evidence import content_hash
from packages.mining.szz import SZZService
from packages.modeling import policy
from packages.modeling.service import ModelService
from packages.persistence.models import (
    AsyncTask,
    DatasetObservation,
    FeatureRun,
    FixAssessment,
    GitCommit,
    Repository,
)
from packages.repositories.parsing import ParsingService
from packages.repositories.storage import Storage
from tests.repository_support import git
from tests.test_szz import execute
from tests.test_tasks import task_env  # noqa: F401

pytestmark = pytest.mark.integration

BASE_STAMP = 1600000000  # 2020-09-13
DAY = 24 * 3600
AS_OF_TIMES = ("2021-06-01", "2022-06-01", "2023-06-01", "2024-06-01")


@pytest.fixture
def obs_env(task_env, tmp_path):  # noqa: F811
    """真实四步管线夹具：40 个修复提交 + 10 个普通提交，跨 2020-09~2024-10。"""
    settings, connections, tasks, actors = task_env
    settings = settings.model_copy(update={"repository_storage_root": tmp_path / "repositories"})
    source = tmp_path / "source"
    source.mkdir()
    git("init", "-b", "main", str(source))
    git("-C", str(source), "config", "user.name", "Observation Fixture")
    git("-C", str(source), "config", "user.email", "obs@example.invalid")
    git("-C", str(source), "config", "core.autocrlf", "false")

    def commit(message, index):
        stamp = BASE_STAMP + index * 30 * DAY
        env = dict(
            os.environ,
            GIT_AUTHOR_DATE=f"{stamp} +0000",
            GIT_COMMITTER_DATE=f"{stamp} +0000",
        )
        git("-C", str(source), "add", "--all", env=env)
        git("-C", str(source), "commit", "--allow-empty", "-m", message, env=env)
        return git("-C", str(source), "rev-parse", "HEAD").decode().strip()

    shas = []
    (source / "code.py").write_text("value = 0\n", encoding="utf-8")
    shas.append(commit("Initial code", 0))
    for index in range(1, 41):
        (source / "code.py").write_text(f"value = {index}\n", encoding="utf-8")
        shas.append(commit("Fix regression", index))
    for index in range(41, 51):
        (source / "notes.txt").write_text(f"note {index}\n", encoding="utf-8")
        shas.append(commit(f"Notes {index}", index))

    repo_id = str(uuid4())
    attempt = Storage(settings).attempt(repo_id, str(uuid4()))
    bare = attempt / "repo.git"
    git("clone", "--bare", str(source), str(bare))
    head = shas[-1]
    with Session(connections.engine) as db, db.begin():
        task = tasks.new_task(
            db,
            actors[1].id,
            str(uuid4()),
            {"repository_id": repo_id},
            "fixture",
            str(uuid4()),
            kind="repository.clone",
        )
        task.status = task.stage = "succeeded"
        task.started_at = datetime(2024, 11, 1, tzinfo=UTC)
        task.finished_at = datetime(2024, 11, 1, 0, 1, tzinfo=UTC)
        task.execution_token = attempt.name
        task.result_json = {"repository_id": repo_id, "head_sha": head}
        db.add(
            Repository(
                id=repo_id,
                owner_id=actors[1].id,
                canonical_url="https://fixture.example/observation/demo.git",
                status="cloned",
                latest_task_id=task.id,
                head_sha=head,
                default_branch="main",
                storage_key=bare.relative_to(settings.repository_storage_root).as_posix(),
                size_bytes=1,
            )
        )
    parser = ParsingService(settings, connections)
    parsing = parser.create(actors[1], repo_id, str(uuid4()), None, str(uuid4()))["task_id"]
    assert execute(parser, settings, parsing)
    fix = FixService(settings, connections)
    fixed = fix.create(
        actors[1],
        repo_id,
        str(uuid4()),
        {"rule_version": "fix-evidence-v1", "include_medium": True},
        str(uuid4()),
    )["task_id"]
    assert execute(fix, settings, fixed)
    with Session(connections.engine) as db:
        assessments = db.scalars(
            select(FixAssessment).where(
                FixAssessment.root_task_id == fixed, FixAssessment.rule_candidate.is_(True)
            )
        ).all()
        ids = [row.id for row in assessments]
    for assessment_id in ids:
        FixService(settings, connections).review(
            actors[1],
            repo_id,
            assessment_id,
            {"status": "confirmed", "expected_revision": 0, "note": "fixture confirm"},
            str(uuid4()),
        )
    with Session(connections.engine) as db, db.begin():
        for row in db.scalars(select(FixAssessment).where(FixAssessment.root_task_id == fixed)):
            commit = db.scalar(
                select(GitCommit).where(
                    GitCommit.repository_id == repo_id, GitCommit.sha == row.sha
                )
            )
            if commit is not None:
                row.created_at = commit.committer_time + timedelta(days=1)
                row.reviewed_at = commit.committer_time + timedelta(days=1)
        db.get(AsyncTask, fixed).finished_at = datetime(2024, 11, 2, tzinfo=UTC)
    szz_roots = []
    for as_of in AS_OF_TIMES:
        szz = SZZService(settings, connections)
        root = szz.create(
            actors[1],
            repo_id,
            str(uuid4()),
            {
                "fix_run_id": fixed,
                "algorithm_version": "baseline-szz-v1",
                "as_of": as_of,
            },
            str(uuid4()),
        )["task_id"]
        assert execute(szz, settings, root)
        with Session(connections.engine) as db, db.begin():
            db.get(AsyncTask, root).finished_at = datetime.fromisoformat(as_of).replace(
                tzinfo=UTC
            )
        szz_roots.append(root)
    features = FeatureService(settings, connections)
    feature_root = features.create(
        actors[1],
        repo_id,
        str(uuid4()),
        {
            "feature_version": "kamei14-dg-v1",
            "purpose": "historical",
            "fix_run_id": fixed,
        },
        str(uuid4()),
    )["task_id"]
    assert execute(features, settings, feature_root)
    yield settings, connections, actors, repo_id, feature_root, szz_roots


def synthetic_audit():
    """与 maturity 内核同构的审计结论（三个分区两类齐备）。"""
    from datetime import datetime as dt

    rows = []
    for index in range(60):
        event = dt.fromtimestamp(BASE_STAMP + index * 30 * DAY, UTC)
        rows.append(
            {
                "sha": f"{index:040x}",
                "event_time": event,
                "label": "buggy" if index % 2 == 0 else "clean",
                "label_available_at": event,
                "reason": None,
                "values": {
                    "NS": 1,
                    "ND": 1,
                    "NF": 1,
                    "Entropy": 0.5,
                    "LA": 5.0,
                    "LD": 3.0,
                    "NDEV": 2,
                    "AGE": float(index + 1),
                    "NUC": 1,
                    "EXP": float(index),
                    "REXP": float(index),
                    "SEXP": float(index),
                    "LT": None,
                    "FIX": "True" if index % 10 == 0 else None,
                },
                "missing_reasons": {
                    "LT": "upstream_source_omits_lt",
                    "FIX": "fix_not_yet_available",
                },
            }
        )
    split = {
        "train": [row["sha"] for row in rows[:24] + rows[40:52]],
        "validation": [row["sha"] for row in rows[24:32] + rows[52:56]],
        "test": [row["sha"] for row in rows[32:40] + rows[56:60]],
    }
    return {
        "ready": True,
        "deficits": [],
        "label_version": "time-labels-v1",
        "feature_version": "kamei14-dg-v1",
        "feature_rule_hash": "0" * 64,
        "parser_version": "native-pydriller-v1",
        "identity_version": "email-normalized-v1",
        "rule_hash": "1" * 64,
        "policy": {
            "snapshot_at": "2026-11-19T00:00:00Z",
            "window_days": 180,
            "train": 0.6,
            "validation": 0.2,
            "test": 0.2,
        },
        "head_sha": "f" * 40,
        "repository_id": str(uuid4()),
        "source_digest": "2" * 64,
        "labels": rows,
        "label_counts": {"buggy": 30, "clean": 30},
        "unknown_reasons": {},
        "split": split,
        "boundaries": {},
        "purged": [],
        "train_class_counts": {"buggy": 18, "clean": 18},
        "train_class_weights": {"buggy": 1.0, "clean": 1.0},
        "train_all_missing_features": [],
        "feature_missing_counts": {"population": {}, "train": {}},
        "preprocessing": {},
    }


def test_members_conversion_kernel():
    result = synthetic_audit()
    members, excluded = DatasetService.members_from_audit(result, "fixture/demo")
    assert excluded == 0
    assert len(members) == 60
    for member in members:
        assert set(member["numeric12"]) == set(research.NUMERIC_FIELDS)
        assert member["lt"] is None
        assert member["lt_missing_reason"] == "upstream_source_omits_lt"
        assert member["fix"] in (None, "True")
        if member["fix"] is None:
            assert member["fix_missing_reason"] == "fix_not_yet_available"
    partitions = {m["partition"] for m in members}
    assert partitions == {"train", "validation", "test"}


def test_members_conversion_excludes_missing_features():
    result = synthetic_audit()
    result["labels"][0]["values"]["AGE"] = None
    result["labels"][0]["missing_reasons"]["AGE"] = "first_modification"
    members, excluded = DatasetService.members_from_audit(result, "fixture/demo")
    assert excluded == 1
    assert len(members) == 59
    assert all(m["commit_id"] != result["labels"][0]["sha"] for m in members)


def request_of(repo_id, feature_root, szz_roots):
    return {
        "name": "observation dataset",
        "source_kind": "repository",
        "repository_id": repo_id,
        "feature_run_id": feature_root,
        "szz_run_ids": szz_roots,
        "purpose": research.PURPOSE,
        "split_strategy": "time_ordered",
        "train_ratio": 0.6,
        "validation_ratio": 0.2,
        "test_ratio": 0.2,
    }


def test_repository_rounds_build_dataset_and_train(obs_env, monkeypatch):
    settings, connections, actors, repo_id, feature_root, szz_roots = obs_env
    parent = assets.ROOT / "runtime/temp"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=parent, prefix="obs-ds-") as name:
        monkeypatch.setattr(research, "OUTPUT", Path(name) / "datasets")
        monkeypatch.setattr(policy, "OUTPUT", Path(name) / "models")
        service = DatasetService(settings, connections)
        request = request_of(repo_id, feature_root, szz_roots)
        binding = service.observation_binding(request)
        members, excluded = service.members_from_audit(synthetic_audit(), "fixture/demo")
        metadata = {
            "version": research.VERSION,
            "rule_hash": "1" * 64,
            "numeric_encoding": "canonical-json-text-roundtrip-v1",
            "policy": {
                k: request[k]
                for k in (
                    "purpose",
                    "split_strategy",
                    "train_ratio",
                    "validation_ratio",
                    "test_ratio",
                )
            },
            "source_hash": binding,
            "asset_id": binding,
            "asset_quality_hash": "2" * 64,
            "source_acquisition": {
                "source_kind": "repository_observation",
                "repository_id": repo_id,
                "feature_run_id": feature_root,
                "szz_run_ids": szz_roots,
                "binding_hash": binding,
            },
            "source_first_stored_at": None,
            "rows": len(members),
            "original_rows": len(members),
            "duplicate_rows": 0,
            "excluded_missing_rows": excluded,
            "unknown_rows": 0,
            "members_hash": content_hash(members),
            "partitions": {
                partition: {
                    "clean": sum(
                        1
                        for m in members
                        if m["partition"] == partition and m["label"] == 0
                    ),
                    "buggy": sum(
                        1
                        for m in members
                        if m["partition"] == partition and m["label"] == 1
                    ),
                }
                for partition in ("train", "validation", "test")
            },
            "repositories": {"fixture/demo": {"clean": 20, "buggy": 40}},
            "cohorts": {"fixture/demo": {"clean": 20, "buggy": 40}},
            "features": list(research.NUMERIC_FIELDS),
            "auxiliary_features": ["fix", "lt"],
            "lt_source": "kamei14_LT_or_missing",
            "target": "mature_SZZ_binary_time_labels_v1",
            "event_clock": "published_author_epoch_UTC",
            "capabilities": {
                "research_binary": True,
                "historical_replay": False,
                "candidate_release": False,
                "research_semantic": False,
                "type_prediction": False,
                "localization": False,
            },
            "limitations": [
                "mature_SZZ_labels_not_gold",
                "not_historical_online_simulation",
                "unknown_labels_not_included",
                "individual_missing_feature_rows_excluded",
                "no_preprocessing_fitted_or_models_trained",
            ],
        }

        def prepared(request_, policy_, check_):
            return members, metadata, content_hash(metadata)

        monkeypatch.setattr(service, "prepare_repository", prepared)
        ack = service.create(actors[1], uuid4().hex, request, str(uuid4()))
        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)
        detail = service.detail(ack["dataset_id"])
        assert detail["ready"], (detail["status"], detail.get("summary"))
        summary = detail["summary"]
        assert summary["source_acquisition"]["source_kind"] == "repository_observation"
        assert summary["target"] == "mature_SZZ_binary_time_labels_v1"
        assert all(
            value["clean"] and value["buggy"] for value in summary["partitions"].values()
        )
        with Session(connections.engine) as db:
            observation = db.scalar(
                select(DatasetObservation).where(
                    DatasetObservation.dataset_version_id == ack["dataset_id"]
                )
            )
            assert observation is not None
            assert observation.repository_id == repo_id
            assert observation.feature_run_id == feature_root
            assert observation.szz_run_ids_json == szz_roots
        replay = service.create(actors[1], uuid4().hex, request, str(uuid4()))
        assert replay["dataset_id"] == ack["dataset_id"]
        models = ModelService(settings, connections)
        mack = models.create(
            actors[1],
            uuid4().hex,
            {"dataset_id": ack["dataset_id"], "plugin": "lr", "params": {}, "seed": 42},
            str(uuid4()),
        )
        mtoken, mpayload = models.tasks.claim(mack["task_id"])
        models.run(mack["task_id"], mtoken, mpayload)
        trained = models.detail(mack["model_run_id"])
        assert trained["registered"]
        assert trained["report"]["calibration_partition"] == "validation"


def test_repository_rounds_refuse_when_audit_not_ready(obs_env, monkeypatch):
    settings, connections, actors, repo_id, feature_root, szz_roots = obs_env
    parent = assets.ROOT / "runtime/temp"
    with TemporaryDirectory(dir=parent, prefix="obs-refuse-") as name:
        monkeypatch.setattr(research, "OUTPUT", Path(name) / "datasets")
        service = DatasetService(settings, connections)
        from packages.mining import dataset as kernel

        original = kernel.audit

        def insufficient(snapshot, vectors, observations, policy, tick=lambda: None):
            result = original(snapshot, vectors, observations, policy, tick)
            result["ready"] = False
            result["deficits"] = ["test_missing_class"]
            result["label_counts"] = {"clean": 5, "unknown": 545, "buggy": 0}
            return result

        monkeypatch.setattr(kernel, "audit", insufficient)
        ack = service.create(
            actors[1], uuid4().hex, request_of(repo_id, feature_root, szz_roots), str(uuid4())
        )
        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)
        with Session(connections.engine) as db:
            task = db.get(AsyncTask, ack["task_id"])
            assert task.status == "failed"
            assert task.error_code == "DATA_MATURE_SAMPLES_INSUFFICIENT"
            assert "不移动切分边界" in (task.error_message or "")


def test_repository_rounds_refuse_when_coverage_incomplete(obs_env, monkeypatch):
    settings, connections, actors, repo_id, feature_root, szz_roots = obs_env
    parent = assets.ROOT / "runtime/temp"
    with TemporaryDirectory(dir=parent, prefix="obs-cover-") as name:
        monkeypatch.setattr(research, "OUTPUT", Path(name) / "datasets")
        service = DatasetService(settings, connections)
        with Session(connections.engine) as db, db.begin():
            db.get(FeatureRun, feature_root).history_coverage = "recent_window"
        ack = service.create(
            actors[1], uuid4().hex, request_of(repo_id, feature_root, szz_roots), str(uuid4())
        )
        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)
        with Session(connections.engine) as db:
            task = db.get(AsyncTask, ack["task_id"])
            assert task.status == "failed"
            assert task.error_code == "DATA_FEATURE_COVERAGE_INCOMPLETE"


def test_repository_rounds_natural_refusal_on_fixture(obs_env, monkeypatch):
    """真实四步管线（单头多观察）下审计如实拒绝：数据不足时不伪造。"""
    settings, connections, actors, repo_id, feature_root, szz_roots = obs_env
    parent = assets.ROOT / "runtime/temp"
    with TemporaryDirectory(dir=parent, prefix="obs-natural-") as name:
        monkeypatch.setattr(research, "OUTPUT", Path(name) / "datasets")
        service = DatasetService(settings, connections)
        ack = service.create(
            actors[1], uuid4().hex, request_of(repo_id, feature_root, szz_roots), str(uuid4())
        )
        token, payload = service.tasks.claim(ack["task_id"])
        service.run(ack["task_id"], token, payload)
        with Session(connections.engine) as db:
            task = db.get(AsyncTask, ack["task_id"])
            assert task.status == "failed"
            assert task.error_code in (
                "DATA_MATURE_SAMPLES_INSUFFICIENT",
                "DATA_OBSERVATION_TIME_UNVERIFIED",
                "DATA_OBSERVATION_INVALID",
            )


def test_observation_selection_validation(obs_env):
    settings, connections, actors, repo_id, feature_root, szz_roots = obs_env
    service = DatasetService(settings, connections)
    base = request_of(repo_id, feature_root, szz_roots)
    for bad in (
        dict(base, szz_run_ids=[]),
        dict(base, szz_run_ids=[szz_roots[0], szz_roots[0]]),
        dict(base, szz_run_ids=["not-a-uuid"]),
        dict(base, feature_run_id=""),
        dict(base, repository_id=""),
    ):
        with pytest.raises(Exception) as exc:
            service.create(actors[1], uuid4().hex, bad, str(uuid4()))
        assert "DATA_OBSERVATION_SELECTION_INVALID" in str(exc.value), bad
