"""Opt-in faults against the dedicated wang deployment, restoring each component."""

import os
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.auth.bootstrap import BootstrapSettings
from packages.persistence.models import AsyncTask, OperationLog, TaskOutbox
from packages.platform.connections import Connections
from packages.tasks.delivery import Dispatcher
from packages.tasks.service import TaskService
from tests.test_runtime import docker, wait_ready
from tests.test_tasks import expire

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1",
        reason="explicit local fault-test opt-in required",
    ),
]


@pytest.fixture(scope="module")
def live():
    settings = BootstrapSettings()
    assert settings.environment == "development" and settings.mysql_host == "127.0.0.1"
    api = httpx.Client(base_url="http://127.0.0.1:18080", timeout=10, trust_env=False)
    response = api.post(
        "/api/v1/auth/login",
        json={
            "username": settings.bootstrap_username,
            "password": settings.bootstrap_password.get_secret_value(),
        },
    )
    assert response.status_code == 201
    api.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    connections = Connections(settings)
    try:
        yield api, settings, connections
    finally:
        api.post("/api/v1/auth/logout", headers={"X-CSRF-Token": response.json()["csrf_token"]})
        api.close()
        connections.close()


def create(api, duration=0, key=None):
    response = api.post(
        "/api/v1/tasks/diagnostic",
        json={"duration_seconds": duration},
        headers={"Idempotency-Key": key or str(uuid4())},
    )
    assert response.status_code == 202
    return response.json()["task_id"]


def wait_task(api, task_id, states, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = api.get("/api/v1/tasks/" + task_id)
        assert response.status_code == 200
        if response.json()["status"] in states:
            return response.json()
        time.sleep(0.5)
    pytest.fail("Task did not reach expected state: " + ",".join(sorted(states)))


def publisher(settings):
    from celery import Celery

    app = Celery("acceptance", broker=settings.broker_url)
    app.conf.update(
        task_default_queue="defectguard",
        task_publish_retry=False,
        broker_transport_options={
            "global_keyprefix": "defectguard:",
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
        },
    )
    return lambda task_id: app.send_task(
        "defectguard.execute", args=[task_id], queue="defectguard", retry=False
    )


def test_real_worker_completion_idempotency_and_duplicate_messages(live):
    api, settings, connections = live
    key = str(uuid4())
    task_id = create(api, 3, key)
    wait_task(api, task_id, {"running"})
    publish = publisher(settings)
    publish(task_id)
    publish(task_id)
    assert create(api, 3, key) == task_id
    result = wait_task(api, task_id, {"succeeded"})
    assert result["result"] == {"duration_seconds": 3, "ok": True} and result["progress"] == 100
    barrier = create(api)
    wait_task(api, barrier, {"succeeded"})
    with Session(connections.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.object_id == task_id, OperationLog.action == "task.start")
            )
            == 1
        )


def test_real_worker_cooperative_cancellation(live):
    api, settings, connections = live
    task_id = create(api, 30)
    wait_task(api, task_id, {"running"})
    assert api.post(f"/api/v1/tasks/{task_id}/cancel").status_code == 202
    result = wait_task(api, task_id, {"cancelled"}, timeout=25)
    assert result["result"] is None
    response = api.post(f"/api/v1/tasks/{task_id}/retry", headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 202 and response.json()["retry_of"] == task_id
    successor = response.json()["task_id"]
    api.post(f"/api/v1/tasks/{successor}/cancel")
    wait_task(api, successor, {"cancelled"}, timeout=25)


def test_real_redis_outage_persists_creation_and_redelivers(live):
    api, settings, connections = live
    try:
        docker("stop", "redis")
        task_id = create(api)
        assert api.get("/api/v1/tasks/" + task_id).json()["status"] == "queued"
        deadline = time.monotonic() + 30
        observed = False
        while time.monotonic() < deadline:
            with Session(connections.engine) as db:
                row = db.scalar(select(TaskOutbox).where(TaskOutbox.task_id == task_id))
                observed = row.attempts > 0 and row.last_error_code == "TASK_BROKER_UNAVAILABLE"
            if observed:
                break
            time.sleep(0.5)
        assert observed, "Actual broker outage was not recorded safely"
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "redis")
    wait_ready()
    assert wait_task(api, task_id, {"succeeded"})["status"] == "succeeded"


def test_real_publish_then_writeback_failure(live, monkeypatch):
    api, settings, connections = live
    try:
        docker("stop", "worker", "scheduler")
        task_id = create(api)
        dispatcher = Dispatcher(settings, connections, publisher(settings))

        def lost_writeback(*args):
            raise RuntimeError("acceptance writeback interruption")

        with monkeypatch.context() as patch:
            patch.setattr(dispatcher, "finish", lost_writeback)
            with pytest.raises(RuntimeError):
                dispatcher.dispatch_once()
        with Session(connections.engine) as db:
            row = db.scalar(select(TaskOutbox).where(TaskOutbox.task_id == task_id))
            assert row.status == "dispatching" and row.attempts == 1
            event_id = row.id
        expire(connections.engine, "task_outbox", "lease_until", event_id)
        assert dispatcher.dispatch_once()
        with Session(connections.engine) as db:
            assert db.get(TaskOutbox, event_id).attempts == 2
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker", "scheduler")
    wait_task(api, task_id, {"succeeded"})
    barrier = create(api)
    wait_task(api, barrier, {"succeeded"})
    with Session(connections.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.object_id == task_id, OperationLog.action == "task.start")
            )
            == 1
        )


def test_real_worker_kill_lease_recovery_and_stale_token(live):
    api, settings, connections = live
    task_id = create(api, 30)
    wait_task(api, task_id, {"running"})
    with Session(connections.engine) as db:
        old_token = db.get(AsyncTask, task_id).execution_token
    try:
        docker("kill", "--signal", "SIGKILL", "worker")
        result = wait_task(api, task_id, {"failed"}, timeout=100)
        assert result["error"]["code"] == "TASK_LEASE_EXPIRED"
        with Session(connections.engine) as db:
            successor = db.scalar(select(AsyncTask).where(AsyncTask.retry_of == task_id))
            assert successor.root_task_id == task_id and successor.retry_count == 1
            successor_id = successor.id
        assert not TaskService(settings, connections).checkpoint(task_id, old_token, complete=True)
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
    wait_task(api, successor_id, {"succeeded"}, timeout=70)


def test_real_coordinator_bounds_queued_wait(live):
    api, settings, connections = live
    try:
        docker("stop", "worker", "scheduler")
        task_id = create(api)
        dispatcher = Dispatcher(settings, connections, publisher(settings))
        assert dispatcher.dispatch_once()
        expire(connections.engine, "async_task", "queued_deadline", task_id)
        docker("up", "-d", "--wait", "--wait-timeout", "120", "scheduler")
        result = wait_task(api, task_id, {"failed"})
        assert result["error"]["code"] == "TASK_QUEUE_TIMEOUT"
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "worker", "scheduler")
    assert api.get("/api/v1/tasks/" + task_id).json()["status"] == "failed"
