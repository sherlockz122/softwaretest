from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from apps.api.application import create_app
from packages.auth.security import PASSWORD_HASHER, CurrentUser
from packages.persistence.models import AsyncTask, OperationLog, TaskOutbox, User
from packages.platform.connections import Connections
from packages.tasks.delivery import Dispatcher
from packages.tasks.service import TaskError, TaskService
from tests.auth_support import migrate, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture
def task_env():
    with scratch_database() as (settings, engine):
        migrate(engine)
        actors = []
        with Session(engine) as db, db.begin():
            for index, role in enumerate(("Admin", "Member", "Viewer", "Member")):
                user = User(
                    id=str(uuid4()),
                    username="task" + str(index),
                    role=role,
                    password_hash=PASSWORD_HASHER.hash("task-fixture-password"),
                    is_active=True,
                )
                db.add(user)
                actors.append(CurrentUser(user.id, user.username, role))
        connections = Connections(settings)
        try:
            yield settings, connections, TaskService(settings, connections), actors
        finally:
            connections.close()


def create(service, actor, key=None, duration=1):
    return service.create(actor, key or str(uuid4()), {"duration_seconds": duration}, str(uuid4()))[
        "task_id"
    ]


def expire(engine, table, column, identifier):
    assert table in {"async_task", "task_outbox"} and column in {
        "lease_until",
        "queued_deadline",
        "next_attempt_at",
    }
    with engine.begin() as db:
        db.execute(
            text(f"UPDATE {table} SET {column}=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=:id"),
            {"id": identifier},
        )


def test_concurrent_idempotency_and_atomic_outbox(task_env):
    settings, connections, service, actors = task_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(service, actors[1], "same-key"), range(2)))
    assert results[0] == results[1]
    with pytest.raises(TaskError) as failure:
        create(service, actors[1], "same-key", 2)
    assert failure.value.code == "TASK_IDEMPOTENCY_CONFLICT"
    with Session(connections.engine) as db:
        task = db.get(AsyncTask, results[0])
        assert task.root_task_id == task.id and task.retry_count == 0
        assert db.scalar(select(func.count()).select_from(AsyncTask)) == 1
        assert db.scalar(select(func.count()).select_from(TaskOutbox)) == 1
        assert db.scalar(select(func.count()).select_from(OperationLog)) == 1


def test_task_database_constraints_and_nonempty_downgrade(task_env):
    settings, connections, service, actors = task_env
    task_id = create(service, actors[1])
    # Verify the frozen migration enforces these rules without the service layer.
    invalid_updates = (
        ("status", "invalid"),
        ("progress", 101),
        ("processed", -1),
        ("retry_count", -1),
        ("root_task_id", str(uuid4())),
        ("payload", '{"oversized":"' + "x" * 17000 + '"}'),
        ("result_json", '{"oversized":"' + "x" * 5000 + '"}'),
    )
    for column, value in invalid_updates:
        with pytest.raises(DBAPIError) as rejected, connections.engine.begin() as db:
            db.execute(
                text(f"UPDATE async_task SET {column}=:value WHERE id=:id"),
                {"value": value, "id": task_id},
            )
        assert rejected.value.orig.args[0] in {1452, 3819}
    with pytest.raises(DBAPIError) as rejected, connections.engine.begin() as db:
        db.execute(text("UPDATE task_outbox SET attempts=-1 WHERE task_id=:id"), {"id": task_id})
    assert rejected.value.orig.args[0] == 3819
    with pytest.raises(IntegrityError), connections.engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO task_outbox (id,task_id,status,next_attempt_at) "
                "VALUES (:id,:task,'pending',UTC_TIMESTAMP(6))"
            ),
            {"id": str(uuid4()), "task": task_id},
        )
    with pytest.raises(RuntimeError, match="Downgrade refused"):
        migrate(connections.engine, "downgrade", "0001_auth")
    with connections.engine.connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002_tasks"
        assert db.execute(text("SELECT COUNT(*) FROM async_task")).scalar() == 1
        assert db.execute(text("SELECT COUNT(*) FROM task_outbox")).scalar() == 1
    migrate(connections.engine, "check")


def test_audit_failure_rolls_back_task_and_outbox(task_env, monkeypatch):
    settings, connections, service, actors = task_env

    def failure(*args):
        raise RuntimeError("private-audit-exception")

    monkeypatch.setattr("packages.tasks.service.audit", failure)
    with pytest.raises(RuntimeError):
        create(service, actors[1])
    with Session(connections.engine) as db:
        assert db.scalar(select(func.count()).select_from(AsyncTask)) == 0
        assert db.scalar(select(func.count()).select_from(TaskOutbox)) == 0


def test_delivery_failure_fencing_and_attempt_limit(task_env):
    settings, connections, service, actors = task_env
    task_id = create(service, actors[1])
    dispatcher = Dispatcher(
        settings.model_copy(update={"delivery_max_attempts": 2}), connections, lambda _: None
    )
    first = dispatcher.claim()
    assert first[1] == task_id
    assert dispatcher.finish(first[0], first[2], False)
    with Session(connections.engine) as db:
        row = db.get(TaskOutbox, first[0])
        assert row.status == "pending" and row.attempts == 1
        assert row.last_error_code == "TASK_BROKER_UNAVAILABLE"
    expire(connections.engine, "task_outbox", "next_attempt_at", first[0])
    second = dispatcher.claim()
    assert second[2] != first[2]
    assert not dispatcher.finish(first[0], first[2], True)
    assert dispatcher.finish(second[0], second[2], False)
    assert service.detail(task_id)["error"]["code"] == "TASK_DISPATCH_FAILED"
    assert dispatcher.claim() is None


def test_sent_then_writeback_lost_duplicate_only_one_claim(task_env):
    settings, connections, service, actors = task_env
    task_id = create(service, actors[1])
    delivered = []
    dispatcher = Dispatcher(settings, connections, delivered.append)
    first = dispatcher.claim()
    delivered.append(first[1])  # published but process stops before finish
    expire(connections.engine, "task_outbox", "lease_until", first[0])
    assert dispatcher.dispatch_once()
    assert delivered == [task_id, task_id]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: service.claim(task_id), range(2)))
    winners = [claim for claim in claims if claim]
    assert len(winners) == 1
    assert service.checkpoint(task_id, winners[0][0], complete=True)
    assert not service.claim(task_id)
    assert not service.checkpoint(task_id, winners[0][0], complete=True)
    assert service.detail(task_id)["status"] == "succeeded"


def test_expired_execution_recovery_chain_and_older_token(task_env):
    settings, connections, service, actors = task_env
    original = create(service, actors[1])
    previous = original
    for attempt in range(settings.task_max_retries + 1):
        token, _ = service.claim(previous)
        expire(connections.engine, "async_task", "lease_until", previous)
        assert not service.checkpoint(previous, token, complete=True)
        assert service.reconcile() == 1
        assert service.detail(previous)["error"]["code"] == "TASK_LEASE_EXPIRED"
        with Session(connections.engine) as db:
            successor = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == previous))
            if attempt < settings.task_max_retries:
                assert successor.root_task_id == original and successor.retry_count == attempt + 1
                previous = successor.id
            else:
                assert successor is None


def test_queue_deadline_and_cancelled_execution_no_recovery(task_env):
    settings, connections, service, actors = task_env
    queued = create(service, actors[1])
    expire(connections.engine, "async_task", "queued_deadline", queued)
    assert service.claim(queued) is None
    service.reconcile()
    assert service.detail(queued)["error"]["code"] == "TASK_QUEUE_TIMEOUT"
    running = create(service, actors[1])
    service.claim(running)
    assert service.cancel(actors[1], running, str(uuid4()))[0] == 202
    expire(connections.engine, "async_task", "lease_until", running)
    service.reconcile()
    assert service.detail(running)["status"] == "cancelled"
    with Session(connections.engine) as db:
        assert db.scalar(select(AsyncTask).where(AsyncTask.retry_of == running)) is None


def test_cancel_complete_race_and_retry_race(task_env):
    settings, connections, service, actors = task_env
    task_id = create(service, actors[1])
    token, _ = service.claim(task_id)

    def cancel():
        try:
            return service.cancel(actors[1], task_id, str(uuid4()))[0]
        except TaskError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation = pool.submit(cancel)
        completion = pool.submit(service.checkpoint, task_id, token, complete=True)
        status, finished = cancellation.result(), completion.result()
    result = service.detail(task_id)["status"]
    assert (result, status, finished) in {("succeeded", 409, True), ("cancelled", 202, False)}
    parent = create(service, actors[1])
    service.cancel(actors[1], parent, str(uuid4()))

    def retry(key):
        try:
            return service.retry(actors[1], parent, key, str(uuid4()))["task_id"]
        except TaskError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(retry, ["a", "b"]))
    assert results.count("TASK_RETRY_EXISTS") == 1
    key = "a" if results[0] != "TASK_RETRY_EXISTS" else "b"
    replay = service.retry(actors[1], parent, key, str(uuid4()))
    assert replay["task_id"] in results
    assert replay["retry_of"] == parent


def test_actual_api_roles_inputs_lists_and_safe_errors(task_env):
    settings, connections, service, actors = task_env
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as api:

        def header(actor):
            response = api.post(
                "/api/v1/auth/login",
                json={"username": actor.username, "password": "task-fixture-password"},
            )
            assert response.status_code == 201
            return {
                "Authorization": "Bearer " + response.json()["access_token"],
                "Idempotency-Key": str(uuid4()),
            }

        member, viewer, other, admin = [header(actors[index]) for index in (1, 2, 3, 0)]
        path = "/api/v1/tasks/diagnostic"
        assert api.post(path, json={}, headers=viewer).status_code == 403
        assert api.post(path, json={}).status_code == 401
        for payload in (
            {"duration_seconds": True},
            {"duration_seconds": 31},
            {"duration_seconds": "1"},
            {"path": "private-never-execute"},
        ):
            response = api.post(path, json=payload, headers=member)
            assert response.status_code == 422 and response.json()["code"] == "TASK_INVALID_INPUT"
            assert "private-never-execute" not in response.text
        response = api.post(path, json={"duration_seconds": 0}, headers=member)
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        for operation in ("cancel", "retry"):
            assert (
                api.post(f"/api/v1/tasks/{task_id}/{operation}", headers=other).status_code == 403
            )
            assert (
                api.post(f"/api/v1/tasks/{task_id}/{operation}", headers=viewer).status_code == 403
            )
        listing = api.get("/api/v1/tasks", headers=viewer).json()
        assert listing["total"] == 1 and listing["items"][0]["id"] == task_id
        assert (
            not {"payload", "payload_hash", "execution_token", "scope_key", "lease_until"}
            & listing["items"][0].keys()
        )
        assert api.get("/api/v1/tasks?page=0", headers=viewer).status_code == 422
        assert api.get("/api/v1/tasks?status=bad", headers=viewer).status_code == 422
        assert api.get("/api/v1/tasks/" + str(uuid4()), headers=viewer).status_code == 404
        assert api.post(f"/api/v1/tasks/{task_id}/cancel", headers=admin).status_code == 204
        assert api.post(f"/api/v1/tasks/{task_id}/cancel", headers=member).status_code == 204
        assert api.post(f"/api/v1/tasks/{task_id}/retry", headers=member).status_code == 202
    with TestClient(
        create_app(settings.model_copy(update={"environment": "production"})),
        base_url="https://127.0.0.1",
    ) as api:
        assert api.post(path, json={}).status_code == 404
