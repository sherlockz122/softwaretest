# ruff: noqa: F811
"""Real Git/MySQL version binding and recovery; no synthetic accuracy claims."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.application import create_app
from apps.api.auth import current_user
from packages.mining.dataset import DatasetError
from packages.mining.dataset_source import DatasetSource
from packages.mining.feature_service import FeatureService
from packages.mining.feature_versions import V2, thaw
from packages.mining.fix import FixService
from packages.persistence.models import (
    AsyncTask,
    FeatureItem,
    FeatureRun,
    FileChange,
    FixAssessment,
    GitCommit,
)
from packages.repositories.parser import ParseExecutor
from packages.repositories.safety import RepositoryError
from tests.test_szz import execute, szz_env  # noqa: F401
from tests.test_tasks import expire, task_env  # noqa: F401

pytestmark = pytest.mark.integration


def start(env, purpose="historical", key=None):
    settings, connections, _, actors, repo, *_ = env
    service = FeatureService(settings, connections)
    root = service.create(
        actors[1],
        repo,
        key or str(uuid4()),
        {"feature_version": V2, "purpose": purpose, "fix_run_id": None},
        str(uuid4()),
    )["task_id"]
    return service, root


def test_v2_git_bound_historical_candidate_same_without_later_review(szz_env):
    settings, connections, _, actors, repo, fixed, shas = szz_env
    service, root = start(szz_env)
    assert execute(service, settings, root)
    historical = service.results(repo, root, page_size=100)
    with Session(connections.engine) as db:
        row = db.scalar(
            select(FixAssessment).where(
                FixAssessment.root_task_id == fixed, FixAssessment.sha == shas[1]
            )
        )
        assessment, revision = row.id, row.review_revision
        inputs = db.scalars(select(FeatureItem).where(FeatureItem.root_task_id == root)).all()
        assert all(
            item.input_json["commit"]["fix"] is None and "message" not in item.input_json["commit"]
            for item in inputs
        )
        assert all(item.input_json["message_purpose"]["message_digest"] for item in inputs)
        assert (
            db.get(FeatureRun, root).fix_json == {}
            and db.get(FeatureRun, root).fix_root_task_id is None
        )
    FixService(settings, connections).review(
        actors[1],
        repo,
        assessment,
        {"status": "rejected", "expected_revision": revision, "note": "after v2 freeze"},
        str(uuid4()),
    )
    next_service, candidate = start(szz_env, "candidate")
    assert execute(next_service, settings, candidate)
    other = service.results(repo, candidate, page_size=100)
    assert historical["quality"] == other["quality"]
    assert historical["items"] == other["items"]
    assert historical["quality"]["feature_version"] == V2
    proof = historical["quality"]["source_validation"]
    assert (
        proof["feature_population_complete"]
        and not proof["label_coverage_complete"]
        and not proof["product_training_ready"]
    )
    assert historical["items"][1]["result"]["values"]["FIX"] == 1
    assert service.results(repo, root, feature="FIX", missing=True)["total"] == 0
    with pytest.raises(DatasetError, match="DATA_SOURCE_CONFLICT"):
        DatasetSource(settings, connections).load(repo, root, [str(uuid4())])


@pytest.mark.parametrize("tamper", ["message", "count", "clock", "identity"])
def test_v2_native_git_rejects_stored_record_drift_before_publication(szz_env, tamper):
    _, connections, _, _, repo, *_ = szz_env
    with Session(connections.engine) as db, db.begin():
        commit = db.scalar(
            select(GitCommit)
            .where(GitCommit.repository_id == repo)
            .order_by(GitCommit.committer_time)
        )
        if tamper == "message":
            commit.message = "Fix forged message"
        elif tamper == "clock":
            from datetime import timedelta

            commit.committer_time += timedelta(seconds=1)
        elif tamper == "identity":
            from packages.persistence.models import AuthorIdentity

            db.get(AuthorIdentity, commit.author_identity_id).identity_key = "a" * 64
        else:
            file = db.scalar(select(FileChange).where(FileChange.commit_id == commit.id))
            file.insertions += 1
    service, root = start(szz_env)
    assert not execute(service, szz_env[0], root)
    with Session(connections.engine) as db:
        assert db.get(AsyncTask, root).error_code == "FEATURE_GIT_RECORD_CONFLICT"
        point = db.get(FeatureRun, root)
        assert point.input_digest is None and point.processed == 0 and point.quality_json is None


@pytest.mark.parametrize("stop", ["cancel", "lease", "capacity"])
def test_v2_partial_recovery_uses_frozen_message_and_fences_old_token(szz_env, monkeypatch, stop):
    settings, connections, _, actors, repo, *_ = szz_env
    service, root = start(szz_env)
    token, payload = service.tasks.claim(root)
    original = service.batch

    def batch(*args, **kw):
        ok = original(*args, **kw)
        if args[2] == 0 and ok:
            if stop == "cancel":
                service.tasks.cancel(actors[1], root, str(uuid4()))
            elif stop == "lease":
                expire(connections.engine, "async_task", "lease_until", root)
            else:
                monkeypatch.setattr(service.storage, "usage", lambda _: SimpleNamespace(free=0))
        return ok

    monkeypatch.setattr(service, "batch", batch)
    assert not ParseExecutor(settings, service).run(root, token, payload)
    monkeypatch.undo()
    with Session(connections.engine) as db:
        source = db.get(FeatureRun, root).source_digest
        assert (
            db.get(FeatureRun, root).processed == 10
            and db.get(FeatureRun, root).quality_json is None
        )
    if stop == "lease":
        service.tasks.reconcile()
    with Session(connections.engine) as db:
        child = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == root))
        child_id = child.id if child else None
    child_id = (
        child_id or service.tasks.retry(actors[1], root, str(uuid4()), str(uuid4()))["task_id"]
    )
    assert not service.heartbeat(root, token)
    assert execute(service, settings, child_id)
    data = service.results(repo, root)
    assert data["quality"]["count"] == 14 and data["run"]["source_digest"] == source


def test_v2_concurrent_idempotency_version_conflict_and_api_roles(szz_env):
    with ThreadPoolExecutor(max_workers=2) as pool:
        roots = list(pool.map(lambda _: start(szz_env, key="same-key")[1], range(2)))
    assert roots[0] == roots[1]
    service = start(szz_env, key="same-key")[0]
    with pytest.raises(RepositoryError, match="TASK_IDEMPOTENCY_CONFLICT"):
        service.create(
            szz_env[3][1],
            szz_env[4],
            "same-key",
            {"feature_version": "kamei14-dg-v1", "purpose": "historical", "fix_run_id": None},
            str(uuid4()),
        )
    app = create_app(szz_env[0])
    app.dependency_overrides[current_user] = lambda: szz_env[3][2]
    with TestClient(app) as client:
        path = f"/api/v1/repositories/{szz_env[4]}"
        assert client.get(path + "/feature-quality", params={"run_id": roots[0]}).status_code == 200
        assert (
            client.post(
                path + "/feature-runs",
                json={"feature_version": V2},
                headers={"Idempotency-Key": "viewer"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                path + "/feature-runs",
                json={"feature_version": V2, "fix_run_id": szz_env[5]},
                headers={"Idempotency-Key": "bad"},
            ).status_code
            == 422
        )


@pytest.mark.parametrize("szz_env", ["recent"], indirect=True)
def test_v2_recent_coverage_not_promoted_to_complete(szz_env):
    service, root = start(szz_env)
    assert execute(service, szz_env[0], root)
    data = service.results(szz_env[4], root)
    assert not data["quality"]["source_validation"]["feature_population_complete"]
    assert data["items"][-1]["result"]["values"]["EXP"] is None


def test_v2_frozen_input_tamper_rejected(szz_env):
    from packages.mining.feature_service import decode_input

    service, root = start(szz_env)
    assert execute(service, szz_env[0], root)
    with Session(szz_env[1].engine) as db:
        data = deepcopy(
            db.scalar(select(FeatureItem).where(FeatureItem.root_task_id == root)).input_json
        )
    data["message_purpose"]["sha"] = "a" * 40
    with pytest.raises(RepositoryError, match="FEATURE_SOURCE_CONFLICT"):
        thaw([data], decode_input)


def test_v2_full_parse_gap_fails_without_claiming_certified_population(szz_env):
    with Session(szz_env[1].engine) as db, db.begin():
        db.scalar(
            select(GitCommit).where(GitCommit.repository_id == szz_env[4])
        ).parser_version = "old"
    service, root = start(szz_env)
    assert not execute(service, szz_env[0], root)
    with Session(szz_env[1].engine) as db:
        assert db.get(AsyncTask, root).error_code == "FEATURE_SOURCE_CONFLICT"
        assert db.get(FeatureRun, root).input_digest is None


def test_v2_rule_drift_cannot_resume_or_publish(szz_env):
    service, root = start(szz_env)
    with Session(szz_env[1].engine) as db, db.begin():
        db.get(FeatureRun, root).rule_hash = "0" * 64
    assert not execute(service, szz_env[0], root)
    with Session(szz_env[1].engine) as db:
        assert db.get(AsyncTask, root).error_code == "FEATURE_SOURCE_CONFLICT"
        assert db.get(FeatureRun, root).quality_json is None
