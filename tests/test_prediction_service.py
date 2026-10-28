"""Prediction service database checks: replay, conflict, queue ordering and point shape."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from packages.auth.security import PASSWORD_HASHER, CurrentUser
from packages.mining.features import FEATURE_VERSION
from packages.persistence.models import (
    AsyncTask,
    DatasetVersion,
    ModelRun,
    PredictionResult,
    PredictionTask,
    Repository,
    User,
)
from packages.platform.connections import Connections
from packages.prediction.policy import PredictionError, VERSION
from packages.prediction.service import PredictionService
from packages.tasks.service import database_now, payload_hash
from tests.auth_support import migrate, scratch_database

pytestmark = pytest.mark.integration

PURPOSE = "historical"
RULES = "0" * 64


@pytest.fixture
def prediction_env():
    with scratch_database() as (settings, engine):
        migrate(engine)
        with Session(engine) as db, db.begin():
            user = User(
                id=str(uuid4()),
                username="prediction",
                role="Member",
                password_hash=PASSWORD_HASHER.hash("prediction-fixture-password"),
                is_active=True,
            )
            db.add(user)
            user_id, username = user.id, user.username
        connections = Connections(settings)
        try:
            yield settings, connections, PredictionService(settings, connections), CurrentUser(
                user_id, username, "Member"
            )
        finally:
            connections.close()


def seed_task(db, actor_id, kind, scope, key, payload, status="succeeded"):
    identifier = str(uuid4())
    task = AsyncTask(
        id=identifier,
        actor_id=actor_id,
        type=kind,
        scope_key=scope,
        idempotency_key=key,
        payload_hash=payload_hash(scope, payload, kind),
        payload=payload,
        status=status,
        stage=status,
        root_task_id=identifier,
        queued_deadline=database_now(db),
        request_id=str(uuid4()),
    )
    db.add(task)
    db.flush()
    return task


def seed_repository(db, actor_id):
    latest = seed_task(db, actor_id, "repository.parse", actor_id + ":repository:parse", "k", {}, "succeeded")
    repo = Repository(
        id=str(uuid4()),
        owner_id=actor_id,
        canonical_url="https://example.com/repo.git",
        status="cloned",
        latest_task_id=latest.id,
    )
    db.add(repo)
    db.flush()
    return repo


def seed_dataset(db, actor_id):
    root = seed_task(db, actor_id, "dataset.build", actor_id + ":dataset.build", "k", {}, "succeeded")
    latest = seed_task(db, actor_id, "dataset.build", actor_id + ":dataset.build:2", "k2", {}, "succeeded")
    dataset = DatasetVersion(
        id=root.id,
        latest_task_id=latest.id,
        name="fixture",
        purpose="training",
        asset_id="0" * 64,
        source_hash="0" * 64,
        policy_json={},
    )
    db.add(dataset)
    db.flush()
    return dataset


def seed_model(db, actor_id, dataset):
    root = seed_task(db, actor_id, "model.train", actor_id + ":model.train", "k", {}, "succeeded")
    latest = seed_task(db, actor_id, "model.train", actor_id + ":model.train:2", "k2", {}, "succeeded")
    run = ModelRun(
        id=root.id,
        latest_task_id=latest.id,
        dataset_id=dataset.id,
        plugin="lr",
        params_json={},
        seed=0,
        code_sha="0" * 64,
        snapshot_json={},
    )
    db.add(run)
    db.flush()
    return run


def seed_point(db, task, repo, run, kind="batch", **extra):
    point = PredictionTask(
        id=task.id,
        repository_id=repo.id,
        model_run_id=run.id,
        base_sha="a" * 40,
        base_ref="main",
        candidate_kind=kind,
        feature_version=FEATURE_VERSION,
        input_digest="0" * 64,
        **extra,
    )
    db.add(point)
    db.flush()
    return point


def seed_result(db, point_id, ordinal, probability, event_time, sha, level="low", effort=10, density=None):
    row = PredictionResult(
        task_id=point_id,
        candidate_ordinal=ordinal,
        candidate_sha=sha,
        event_time=event_time,
        probability=probability,
        threshold=0.5,
        risk_level=level,
        effort=effort,
        risk_density=density if density is not None else probability / max(1, effort),
        oversized=effort > 500,
        numeric_json={},
        reasons_json={},
        explanation_json={"method": "lr-scaled-coefficient-v1", "factors": []},
        input_json={},
    )
    db.add(row)
    db.flush()
    return row


def seed_chain(prediction_env, kind="batch", status="succeeded"):
    settings, connections, service, actor = prediction_env
    with Session(connections.engine) as db, db.begin():
        repo = seed_repository(db, actor.id)
        dataset = seed_dataset(db, actor.id)
        run = seed_model(db, actor.id, dataset)
        task = seed_task(
            db,
            actor.id,
            "prediction." + ("batch" if kind == "batch" else "candidate"),
            actor.id + ":prediction." + ("batch" if kind == "batch" else "candidate"),
            "key-" + str(uuid4())[:8],
            {"prediction_version": VERSION},
            status=status,
        )
        point = seed_point(db, task, repo, run, kind=kind)
        return SimpleNamespace(
            task_id=task.id,
            point_id=point.id,
            model_run_id=run.id,
        )


def test_idempotent_replay_returns_single_response(prediction_env):
    settings, connections, service, actor = prediction_env
    payload = {
        "prediction_version": VERSION,
        "repository_id": str(uuid4()),
        "model_run_id": str(uuid4()),
        "base_ref": "main",
        "candidate_ref": "feature/x",
        "candidate_diff": None,
    }
    scope = actor.id + ":prediction.candidate"
    with Session(connections.engine) as db, db.begin():
        task = seed_task(db, actor.id, "prediction.candidate", scope, "replay-key", payload, "succeeded")
        repo = seed_repository(db, actor.id)
        dataset = seed_dataset(db, actor.id)
        run = seed_model(db, actor.id, dataset)
        point = seed_point(db, task, repo, run, kind="single_ref", base_time=datetime(2026, 1, 1))
        seed_result(db, point.id, 0, 0.73, datetime(2026, 1, 1), "b" * 40, level="high")
        run_id = run.id
    request = dict(payload)
    del request["prediction_version"]
    response = service.create_single(actor, "replay-key", request, str(uuid4()))
    assert response["prediction_id"]
    assert response["probability"] == 0.73 and response["risk_level"] == "high"
    assert response["model_run_id"] == run_id
    assert response["top_factors"] == []
    assert len(response["limitations"]) == 1


def test_same_key_different_payload_conflicts(prediction_env):
    settings, connections, service, actor = prediction_env
    payload = {
        "prediction_version": VERSION,
        "repository_id": str(uuid4()),
        "model_run_id": str(uuid4()),
        "base_ref": "main",
        "candidate_ref": "feature/x",
        "candidate_diff": None,
    }
    scope = actor.id + ":prediction.candidate"
    with Session(connections.engine) as db, db.begin():
        seed_task(db, actor.id, "prediction.candidate", scope, "conflict-key", payload, "succeeded")
    request = dict(payload)
    del request["prediction_version"]
    request["base_ref"] = "develop"
    with pytest.raises(PredictionError) as failure:
        service.create_single(actor, "conflict-key", request, str(uuid4()))
    assert failure.value.code == "TASK_IDEMPOTENCY_CONFLICT" and failure.value.status == 409


def test_queue_orders_probability_then_time_then_sha(prediction_env):
    settings, connections, service, actor = prediction_env
    chain = seed_chain(prediction_env)
    rows = [
        (0.2, datetime(2026, 1, 3), "c" * 40, "low"),
        (0.7, datetime(2026, 1, 2), "d" * 40, "high"),
        (0.7, datetime(2026, 1, 2), "e" * 40, "high"),
        (0.7, datetime(2026, 1, 1), "f" * 40, "high"),
        (0.9, datetime(2026, 1, 4), "a" * 40, "high"),
    ]
    with Session(connections.engine) as db, db.begin():
        for ordinal, (probability, event_time, sha, level) in enumerate(rows):
            seed_result(db, chain.point_id, ordinal, probability, event_time, sha, level=level)
    response = service.queue(chain.task_id)
    assert [item["candidate_sha"] for item in response["items"]] == [
        "a" * 40,
        "f" * 40,
        "d" * 40,
        "e" * 40,
        "c" * 40,
    ]
    assert response["total"] == 5 and response["threshold"] == 0.5
    assert response["empty"] is False
    assert [item["probability"] for item in response["items"]] == [0.9, 0.7, 0.7, 0.7, 0.2]


def test_queue_level_and_oversized_filters(prediction_env):
    settings, connections, service, actor = prediction_env
    chain = seed_chain(prediction_env)
    with Session(connections.engine) as db, db.begin():
        seed_result(db, chain.point_id, 0, 0.9, datetime(2026, 1, 1), "a" * 40, level="high", effort=600)
        seed_result(db, chain.point_id, 1, 0.4, datetime(2026, 1, 2), "b" * 40, level="medium")
        seed_result(db, chain.point_id, 2, 0.1, datetime(2026, 1, 3), "c" * 40, level="low", effort=601)
    assert service.queue(chain.task_id, level="high")["total"] == 1
    assert service.queue(chain.task_id, level="medium")["total"] == 1
    assert service.queue(chain.task_id, oversized=True)["total"] == 2
    assert service.queue(chain.task_id, oversized=False)["total"] == 1
    item = service.queue(chain.task_id, oversized=True)["items"][0]
    assert item["oversized"] is True and item["risk_density"] == pytest.approx(0.9 / 600, abs=1e-6)


def test_queue_empty_batch_and_missing_task(prediction_env):
    settings, connections, service, actor = prediction_env
    chain = seed_chain(prediction_env)
    response = service.queue(chain.task_id)
    assert response["total"] == 0 and response["empty"] is True
    assert response["threshold"] is None
    with pytest.raises(PredictionError) as failure:
        service.queue(str(uuid4()))
    assert failure.value.code == "PRED_NOT_FOUND" and failure.value.status == 404


def test_explanation_returns_method_factors_and_disclaimer(prediction_env):
    settings, connections, service, actor = prediction_env
    chain = seed_chain(prediction_env)
    factors = [
        {"feature": "ld", "value": 12.0, "contribution": 0.4, "direction": "increase"},
        {"feature": "la", "value": 3.0, "contribution": -0.1, "direction": "decrease"},
    ]
    with Session(connections.engine) as db, db.begin():
        row = seed_result(db, chain.point_id, 0, 0.6, datetime(2026, 1, 1), "b" * 40, level="high")
        row.explanation_json = {"method": "lr-scaled-coefficient-v1", "factors": factors}
        row_id = row.id
    response = service.explanation(row_id)
    assert response["method"] == "lr-scaled-coefficient-v1"
    assert response["factors"] == factors
    assert response["model_run_id"] == chain.model_run_id
    assert response["feature_version"] == FEATURE_VERSION
    assert len(response["limitations"]) == 1
    with pytest.raises(PredictionError) as failure:
        service.explanation(str(uuid4()))
    assert failure.value.code == "PRED_NOT_FOUND"


def test_point_classification_for_all_kinds(prediction_env):
    settings, connections, service, actor = prediction_env
    with Session(connections.engine) as db, db.begin():
        repo = seed_repository(db, actor.id)
        dataset = seed_dataset(db, actor.id)
        run = seed_model(db, actor.id, dataset)
        feature_run = SimpleNamespace(root_task_id=seed_task(
            db, actor.id, "repository.feature", actor.id + ":repository:feature", "kf", {}, "succeeded"
        ).id)
        base = {
            "repository_id": repo.id,
            "model_run_id": run.id,
            "base_ref": "main",
        }
        batch_task = seed_task(db, actor.id, "prediction.batch", actor.id + ":prediction.batch", "kb", {}, "queued")
        point = service._point(
            db,
            batch_task,
            {**base, "window_days": 30, "max_candidates": 10},
            feature_run.root_task_id,
            None,
        )
        assert point.candidate_kind == "batch"
        assert point.candidate_json == {"window_days": 30, "max_candidates": 10}
        assert point.base_sha == "" and point.candidate_sha is None
        ref_task = seed_task(db, actor.id, "prediction.candidate", actor.id + ":prediction.candidate", "kr", {}, "queued")
        point = service._point(
            db,
            ref_task,
            {**base, "candidate_ref": "feature/x", "candidate_diff": None},
            feature_run.root_task_id,
            None,
        )
        assert point.candidate_kind == "single_ref" and point.candidate_json is None
        diff_task = seed_task(db, actor.id, "prediction.candidate", actor.id + ":prediction.candidate:2", "kd", {}, "queued")
        diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        point = service._point(
            db,
            diff_task,
            {**base, "candidate_ref": None, "candidate_diff": diff},
            feature_run.root_task_id,
            None,
        )
        assert point.candidate_kind == "single_diff"
        assert point.candidate_json == {"diff": diff}
