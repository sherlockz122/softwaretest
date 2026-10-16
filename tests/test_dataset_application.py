"""Real isolated MySQL/RBAC/lease/artifact acceptance; synthetic rows are not gold."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from apps.api.application import create_app
from packages.auth.security import PASSWORD_HASHER, CurrentUser
from packages.mining import dataset_service as module
from packages.mining import public_assets as assets
from packages.mining import research_dataset as research
from packages.mining.dataset_service import DatasetService
from packages.persistence.models import AsyncTask, DatasetMember, User
from packages.platform.connections import Connections
from packages.tasks.service import TaskError, database_now
from tests.auth_support import migrate, scratch_database
from tests.dataset_support import fixture_asset


@pytest.fixture(scope="module")
def database():
    with scratch_database() as (settings, engine):
        migrate(engine)
        with Session(engine) as db, db.begin():
            users = []
            for role in ("Viewer", "Member", "Admin", "Member"):
                user = User(
                    id=str(uuid4()),
                    username="ds-" + uuid4().hex[:12],
                    role=role,
                    password_hash=PASSWORD_HASHER.hash("test-only-dataset-password"),
                    is_active=True,
                )
                db.add(user)
                users.append((CurrentUser(user.id, user.username, role), user.username))
        yield settings, engine, users


@pytest.fixture
def environment(database, monkeypatch):
    settings, engine, users = database
    root = assets.ROOT / "runtime/temp"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root, prefix="dataset-db-") as path:
        work = Path(path)
        directory, source = fixture_asset(work)
        monkeypatch.setattr(research, "STORE", directory.parent)
        monkeypatch.setattr(research, "OUTPUT", work / "output")
        connections = Connections(settings)
        try:
            service = DatasetService(settings, connections)
            body = {
                "name": "acceptance research",
                "asset_id": directory.name,
                "source_hash": source["manifest"]["content_hash"],
                "purpose": research.PURPOSE,
                "split_strategy": "time_ordered",
                "train_ratio": 0.70,
                "validation_ratio": 0.15,
                "test_ratio": 0.15,
            }
            yield service, body, users, settings, engine, directory
        finally:
            connections.close()


def create_and_claim(environment):
    service, body, users, _, _, _ = environment
    ack = service.create(users[1][0], uuid4().hex, body, str(uuid4()))
    token, payload = service.tasks.claim(ack["task_id"])
    return ack, token, payload


def test_idempotent_root_and_request_conflict(environment):
    service, body, users, _, _, _ = environment
    key = uuid4().hex
    first = service.create(users[1][0], key, body, str(uuid4()))
    assert service.create(users[1][0], key, body, str(uuid4())) == first
    with pytest.raises(TaskError, match="TASK_IDEMPOTENCY_CONFLICT"):
        service.create(users[1][0], key, body | {"name": "different"}, str(uuid4()))
    assert service.detail(first["dataset_id"])["members"] == []
    with pytest.raises(TaskError, match="DATA_NOT_READY"):
        service.download(first["dataset_id"], "train")


def test_real_batches_ready_csv_and_member_identity(environment):
    service = environment[0]
    ack, token, payload = create_and_claim(environment)
    service.run(ack["task_id"], token, payload)
    result = service.detail(ack["dataset_id"])
    assert result["ready"] and result["total"] == result["processed"] == 40
    assert result["summary"]["partitions"]["train"] == {"clean": 14, "buggy": 14}
    csv = service.download(ack["dataset_id"], "train").read_text()
    assert len(csv.splitlines()) == 29 and ",fix," not in csv.splitlines()[0]
    assert result["members"][0]["numeric12"] == {k: 1.0 for k in research.NUMERIC_FIELDS}
    assert not service.tasks.claim(ack["task_id"])


def test_fault_retry_resumes_and_old_token_cannot_publish(environment, monkeypatch):
    service, _, users, _, engine, _ = environment
    monkeypatch.setattr(module, "BATCH_SIZE", 10)
    ack, token, payload = create_and_claim(environment)

    def fault(_):
        raise RuntimeError("simulated worker exit after commit")

    with pytest.raises(RuntimeError):
        service.run(ack["task_id"], token, payload, after_batch=fault)
    assert service.detail(ack["dataset_id"])["processed"] == 10
    with Session(engine) as db, db.begin():
        task = db.get(AsyncTask, ack["task_id"])
        task.lease_until = database_now(db) - timedelta(seconds=1)
    assert service.tasks.reconcile() >= 1
    with Session(engine) as db:
        retry = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == ack["task_id"]))
        retry_id = retry.id
    new_token, new_payload = service.tasks.claim(retry_id)
    assert not service.batch(ack["task_id"], token, 10, [], "0" * 64)
    service.run(retry_id, new_token, new_payload)
    result = service.detail(ack["dataset_id"])
    assert result["ready"] and len(result["members"]) == 40 and result["task_id"] == retry_id
    assert service.tasks.listing("dataset.build")["total"] >= 2


def test_cancel_after_batch_does_not_publish(environment, monkeypatch):
    service, _, users, _, _, _ = environment
    monkeypatch.setattr(module, "BATCH_SIZE", 10)
    ack, token, payload = create_and_claim(environment)

    def cancel(_):
        service.tasks.cancel(users[1][0], ack["task_id"], str(uuid4()))

    service.run(ack["task_id"], token, payload, after_batch=cancel)
    assert service.tasks.detail(ack["task_id"])["status"] == "cancelled"
    assert not service.detail(ack["dataset_id"])["ready"]
    with pytest.raises(TaskError, match="DATA_NOT_READY"):
        service.download(ack["dataset_id"], "train")


def test_source_drift_refused(environment):
    service, _, users, _, engine, directory = environment
    ack, token, payload = create_and_claim(environment)
    path = directory / "normalized.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    service.run(ack["task_id"], token, payload)
    assert service.tasks.detail(ack["task_id"])["status"] == "failed"
    assert not service.detail(ack["dataset_id"])["ready"]


def test_download_artifact_drift_refused(environment):
    service = environment[0]
    ack, token, payload = create_and_claim(environment)
    service.run(ack["task_id"], token, payload)
    path = service.download(ack["dataset_id"], "train")
    path.write_bytes(path.read_bytes() + b"modified")
    with pytest.raises(research.ResearchError, match="DATA_ARTIFACT_DRIFT"):
        service.download(ack["dataset_id"], "train")


def test_api_roles_routes_csv_and_owner_guard(environment):
    service, body, users, settings, _, _ = environment
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/datasets").status_code == 401

        def login(index):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": users[index][1], "password": "test-only-dataset-password"},
            )
            assert response.status_code == 201
            return {
                "Authorization": "Bearer " + response.json()["access_token"],
                "Idempotency-Key": uuid4().hex,
            }

        viewer = login(0)
        assert client.get("/api/v1/datasets", headers=viewer).status_code == 200
        assert client.post("/api/v1/datasets", headers=viewer, json=body).status_code == 403
        member = login(1)
        response = client.post("/api/v1/datasets", headers=member, json=body)
        assert response.status_code == 202
        result = response.json()
        other = login(3)
        assert (
            client.post(f"/api/v1/tasks/{result['task_id']}/cancel", headers=other).status_code
            == 403
        )
        token, payload = service.tasks.claim(result["task_id"])
        service.run(result["task_id"], token, payload)
        assert client.get(f"/api/v1/datasets/{result['dataset_id']}", headers=viewer).json()[
            "ready"
        ]
        exported = client.get(
            f"/api/v1/datasets/{result['dataset_id']}?format=csv&partition=test", headers=viewer
        )
        assert exported.status_code == 200 and len(exported.text.splitlines()) == 7
        assert "text/csv" in exported.headers["content-type"]
        assert (
            client.post(
                "/api/v1/datasets", headers=member, json=body | {"path": "C:/outside"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/datasets", headers=member, json=body | {"purpose": "historical"}
            ).status_code
            == 422
        )


def test_empty_migration_and_whole_downgrade_guard():
    with scratch_database() as (_, engine):
        migrate(engine)
        migrate(engine, "check")
        assert "dataset_version" in inspect(engine).get_table_names()
        migrate(engine, "downgrade", "0008_features")
        assert "dataset_version" not in inspect(engine).get_table_names()
        migrate(engine)
        with Session(engine) as db, db.begin():
            db.add(
                User(
                    id=str(uuid4()),
                    username="downgrade-dataset",
                    role="Viewer",
                    password_hash="inert-test-hash",
                    is_active=True,
                )
            )
        with pytest.raises(RuntimeError, match="Downgrade refused"):
            migrate(engine, "downgrade", "base")
        assert "dataset_version" in inspect(engine).get_table_names()


def test_resume_refuses_changed_member(environment, monkeypatch):
    service, _, users, _, engine, _ = environment
    monkeypatch.setattr(module, "BATCH_SIZE", 10)
    ack, token, payload = create_and_claim(environment)

    def fault(_):
        raise RuntimeError("worker exit")

    with pytest.raises(RuntimeError):
        service.run(ack["task_id"], token, payload, after_batch=fault)
    with Session(engine) as db, db.begin():
        row = db.get(DatasetMember, (ack["dataset_id"], 0))
        row.numeric_json = row.numeric_json | {"la": 999}
        task = db.get(AsyncTask, ack["task_id"])
        task.lease_until = database_now(db) - timedelta(seconds=1)
    service.tasks.reconcile()
    with Session(engine) as db:
        retry_id = db.scalar(select(AsyncTask.id).where(AsyncTask.retry_of == ack["task_id"]))
    new_token, new_payload = service.tasks.claim(retry_id)
    service.run(retry_id, new_token, new_payload)
    task = service.tasks.detail(retry_id)
    assert task["status"] == "failed" and task["error"]["code"] == "DATA_MEMBER_DRIFT"
    assert not service.detail(ack["dataset_id"])["ready"]


def test_publish_transaction_failure_is_not_ready_and_recovers(environment):
    service, _, _, _, engine, _ = environment
    ack, token, payload = create_and_claim(environment)

    def fail_publish(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE dataset_version") and "output_json" in statement:
            raise RuntimeError("injected final publication failure")

    event.listen(service.engine, "before_cursor_execute", fail_publish)
    try:
        with pytest.raises(RuntimeError, match="injected final"):
            service.run(ack["task_id"], token, payload)
    finally:
        event.remove(service.engine, "before_cursor_execute", fail_publish)
    assert not service.detail(ack["dataset_id"])["ready"]
    assert service.tasks.detail(ack["task_id"])["status"] == "running"
    with Session(engine) as db, db.begin():
        task = db.get(AsyncTask, ack["task_id"])
        task.lease_until = database_now(db) - timedelta(seconds=1)
    service.tasks.reconcile()
    with Session(engine) as db:
        retry_id = db.scalar(select(AsyncTask.id).where(AsyncTask.retry_of == ack["task_id"]))
    next_token, next_payload = service.tasks.claim(retry_id)
    service.run(retry_id, next_token, next_payload)
    assert service.detail(ack["dataset_id"])["ready"]


def test_whole_build_deadline_refuses_publication(environment, monkeypatch):
    service = environment[0]
    ack, token, payload = create_and_claim(environment)
    monkeypatch.setattr(module, "MAX_BUILD_SECONDS", 0)
    service.run(ack["task_id"], token, payload)
    task = service.tasks.detail(ack["task_id"])
    assert task["status"] == "failed" and task["error"]["code"] == "DATA_BUILD_DEADLINE"
    assert not service.detail(ack["dataset_id"])["ready"]


def test_high_precision_numbers_round_trip_without_tolerance(environment):
    service, body, users, _, engine, directory = environment

    def precise(row, _, project):
        row["ent"] = "1.2068478319973799"
        row["age"] = "18.181818181818198"

    alternate, source = fixture_asset(directory.parent.parent / "precise", mutate=precise)
    service.store = alternate.parent
    body = body | {"asset_id": alternate.name, "source_hash": source["manifest"]["content_hash"]}
    ack = service.create(users[1][0], uuid4().hex, body, str(uuid4()))
    token, payload = service.tasks.claim(ack["task_id"])
    service.run(ack["task_id"], token, payload)
    result = service.detail(ack["dataset_id"])
    assert result["ready"]
    number = result["members"][0]["numeric12"]
    assert number["ent"].hex() == float("1.2068478319973799").hex()
    assert number["age"].hex() == float("18.181818181818198").hex()
    with pytest.raises(RuntimeError, match="Downgrade refused"):
        migrate(engine, "downgrade", "0009_datasets")
    column = next(
        c for c in inspect(engine).get_columns("dataset_member") if c["name"] == "numeric_json"
    )
    assert column["type"].__class__.__name__ == "MEDIUMTEXT"


def test_capacity_checks_output_filesystem_before_new_work(environment, monkeypatch):
    service = environment[0]
    ack, token, payload = create_and_claim(environment)
    probed = []

    def low_capacity(path):
        probed.append(path)
        return SimpleNamespace(free=2 * 1024**3)

    monkeypatch.setattr(module.shutil, "disk_usage", low_capacity)
    service.run(ack["task_id"], token, payload)
    task = service.tasks.detail(ack["task_id"])
    assert task["status"] == "failed" and task["error"]["code"] == "DATA_CAPACITY_UNSAFE"
    assert probed[0] == service.output.parent
    assert not service.detail(ack["dataset_id"])["ready"]
