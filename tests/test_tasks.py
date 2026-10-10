"""INT-OUTBOX / REL-LEASE / REL-CANCEL / SEC-RBAC：可靠任务集成测试（docs/04 第 10.2～10.5 节）。

覆盖 A35 诊断创建、outbox 同事务与投递、真实执行原语（领取/心跳/条件写回）、
恢复协调器（租约过期/队列超时/自动恢复上限）、A29～A32 以及任务操作者 RBAC。
需要运行中的 MySQL 与 Redis（pytest -m integration）。
"""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pymysql
import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from apps.api.application import create_app
from apps.worker.main import create_celery_app
from apps.worker.tasks import execute_task
from packages.domain.models import AsyncTask, AuthSession, OperationLog, TaskOutbox, User
from packages.platform.config import load_settings
from packages.security.passwords import hash_password
from packages.security.tokens import as_utc, now_utc
from packages.tasks import dispatcher as dispatcher_module
from packages.tasks.coordinator import run_coordinator_cycle
from packages.tasks.dispatcher import run_dispatch_cycle
from packages.tasks.executor import claim_task, finish_task, heartbeat_task
from packages.tasks.service import create_task

pytestmark = pytest.mark.integration

ORIGIN = "http://tasks.test.local"
LOGIN_URL = "/api/v1/auth/login"
DIAGNOSTICS_URL = "/api/v1/tasks/diagnostics"
TASKS_URL = "/api/v1/tasks"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_TEST_DATABASE = "defectguard_task_test"


def _dotenv_value(name: str) -> str:
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{name} missing in .env")


@pytest.fixture(scope="module")
def client():
    # 独立临时库隔离：live 容器投递循环与 runtime 遗留任务不会干扰本套件的计数断言。
    settings = load_settings()
    connection = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user="root",
        password=_dotenv_value("DG_MYSQL_ROOT_PASSWORD"),
        autocommit=True,
    )
    with connection.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{TASK_TEST_DATABASE}`")
        cursor.execute(
            f"CREATE DATABASE `{TASK_TEST_DATABASE}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.execute(
            f"GRANT ALL PRIVILEGES ON `{TASK_TEST_DATABASE}`.* "
            f"TO `{settings.mysql_user}`@'%'"
        )
    connection.close()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DG_MYSQL_DATABASE": TASK_TEST_DATABASE},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
    settings = load_settings(
        mysql_database=TASK_TEST_DATABASE,
        frontend_origin=ORIGIN,
        task_loops_enabled=False,
        broker_redis_db=15,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_broker_and_limits(client):
    settings = client.app.state.settings
    broker = redis_lib.Redis(
        host=settings.redis_host, port=settings.redis_port,
        password=settings.redis_password.get_secret_value(), db=settings.broker_redis_db,
    )
    broker.flushdb()
    rate = client.app.state.connections.redis
    for key in rate.scan_iter(match="defectguard:login:*"):
        rate.delete(key)
    client.cookies.clear()
    yield
    broker.flushdb()


@pytest.fixture()
def make_user(client):
    created_user_ids = []

    def factory(*, username: str, password: str, role: str = "Member",
                active: bool = True) -> str:
        with client.app.state.session_factory() as session:
            user = User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                is_active=active,
            )
            session.add(user)
            session.commit()
            user_id = user.id
        created_user_ids.append(user_id)
        return user_id

    yield factory

    with client.app.state.session_factory() as session:
        task_ids = session.execute(
            select(AsyncTask.id).where(AsyncTask.actor_id.in_(created_user_ids))
        ).scalars().all()
        if task_ids:
            session.execute(
                delete(TaskOutbox).where(TaskOutbox.task_id.in_(task_ids))
            )
            # retry_of 外键：先删后继（retry_count 高），再删根任务
            for level in range(5, -1, -1):
                session.execute(
                    delete(AsyncTask).where(
                        AsyncTask.id.in_(task_ids), AsyncTask.retry_count == level
                    )
                )
        for user_id in created_user_ids:
            session.execute(delete(OperationLog).where(OperationLog.actor_id == user_id))
            session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            session.execute(delete(User).where(User.id == user_id))
        session.commit()


def _login(client, username, password):
    return client.post(LOGIN_URL, json={"username": username, "password": password})


def _auth_headers(client, username, password):
    response = _login(client, username, password)
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@contextmanager
def _db(client):
    with client.app.state.session_factory() as session:
        yield session


def _get_task(client, task_id) -> dict:
    with _db(client) as session:
        return session.execute(
            select(AsyncTask).where(AsyncTask.id == task_id)
        ).scalar_one()


def _create_diagnostics(client, headers, *, key, duration=1):
    return client.post(
        DIAGNOSTICS_URL,
        json={"duration_seconds": duration},
        headers={**headers, "Idempotency-Key": key},
    )


# ---------------------------------------------------------------- A35 与幂等

def test_diagnostics_create_contract_and_idempotency(client, make_user):
    make_user(username="taskuser1", password="taskuser1-pass-123456")
    headers = _auth_headers(client, "taskuser1", "taskuser1-pass-123456")
    first = _create_diagnostics(client, headers, key="diag-key-1", duration=1)
    assert first.status_code == 202
    body = first.json()
    assert body["status"] == "queued"
    assert body["task_id"]
    assert set(body) == {"task_id", "status"}

    replay = _create_diagnostics(client, headers, key="diag-key-1", duration=1)
    assert replay.status_code == 202
    assert replay.json()["task_id"] == body["task_id"]  # 同键同载荷返回原任务

    with _db(client) as session:
        tasks = session.execute(
            select(AsyncTask).where(AsyncTask.scope_key.like(f"user:{_me(client, headers)}%"))
        ).scalars().all()
        assert len(tasks) == 1  # 数据库仅一个任务
        outboxes = session.execute(
            select(TaskOutbox).where(TaskOutbox.task_id == body["task_id"])
        ).scalars().all()
        assert len(outboxes) == 1  # 一个初始 outbox 事件
        assert outboxes[0].status == "pending"
        assert outboxes[0].attempts == 0
        audit = session.execute(
            select(OperationLog).where(
                OperationLog.object_id == body["task_id"], OperationLog.action == "task.create"
            )
        ).scalar_one()
        assert audit.result == "succeeded"


def _me(client, headers) -> str:
    return client.get("/api/v1/users/me", headers=headers).json()["id"]


def test_diagnostics_same_key_different_payload_conflict(client, make_user):
    make_user(username="taskuser2", password="taskuser2-pass-123456")
    headers = _auth_headers(client, "taskuser2", "taskuser2-pass-123456")
    assert _create_diagnostics(client, headers, key="diag-key-2", duration=1).status_code == 202
    conflict = _create_diagnostics(client, headers, key="diag-key-2", duration=5)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "TASK_IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("key", ["", "x" * 129, "del\x7fchar", "tab\tkey"])
def test_diagnostics_invalid_idempotency_key(client, make_user, key):
    make_user(username="taskuser3", password="taskuser3-pass-123456")
    headers = _auth_headers(client, "taskuser3", "taskuser3-pass-123456")
    response = client.post(
        DIAGNOSTICS_URL, json={"duration_seconds": 1},
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "TASK_INVALID_INPUT"


@pytest.mark.parametrize("body", [
    {"duration_seconds": -1},
    {"duration_seconds": 31},
    {"duration_seconds": 1.5},
    {"duration_seconds": True},
    {"duration_seconds": "1"},
    {"duration_seconds": 1, "command": "rm -rf /"},
])
def test_diagnostics_invalid_body(client, make_user, body):
    make_user(username="taskuser4", password="taskuser4-pass-123456")
    headers = _auth_headers(client, "taskuser4", "taskuser4-pass-123456")
    response = client.post(
        DIAGNOSTICS_URL, json=body, headers={**headers, "Idempotency-Key": "diag-key-4"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "TASK_INVALID_INPUT"


def test_diagnostics_default_duration(client, make_user):
    make_user(username="taskuser5", password="taskuser5-pass-123456")
    headers = _auth_headers(client, "taskuser5", "taskuser5-pass-123456")
    response = client.post(
        DIAGNOSTICS_URL, json={}, headers={**headers, "Idempotency-Key": "diag-key-5"}
    )
    assert response.status_code == 202
    with _db(client) as session:
        task = session.get(AsyncTask, response.json()["task_id"])
        assert task.payload == {"duration_seconds": 1}


def test_diagnostics_hidden_in_production():
    settings = load_settings(environment="production", frontend_origin=ORIGIN,
                             task_loops_enabled=False)
    with TestClient(create_app(settings)) as prod_client:
        response = prod_client.post(
            DIAGNOSTICS_URL, json={}, headers={"Idempotency-Key": "diag-key-prod"}
        )
        assert response.status_code == 404
        assert response.json()["code"] == "SYSTEM_NOT_FOUND"


def test_create_task_transaction_rollback(client, make_user, monkeypatch):
    user_id = make_user(username="taskuser6", password="taskuser6-pass-123456")
    with _db(client) as session:
        settings = client.app.state.settings

        def explode(*args, **kwargs):
            raise RuntimeError("injected audit failure")

        monkeypatch.setattr("packages.tasks.service.add_audit_log", explode)
        with pytest.raises(RuntimeError, match="injected"):
            create_task(
                session, settings, actor_id=user_id, task_type="diagnostics",
                scope_key=f"user:{user_id}:diagnostics", idempotency_key="diag-key-6",
                payload={"duration_seconds": 1}, api_version="v1", request_id=None,
            )
    with _db(client) as session:
        rows = session.execute(
            select(AsyncTask).where(AsyncTask.idempotency_key == "diag-key-6")
        ).scalars().all()
        assert rows == []  # 任务与 outbox 未部分落库
        assert session.execute(
            select(TaskOutbox).where(TaskOutbox.task_id.in_(
                select(AsyncTask.id).where(AsyncTask.idempotency_key == "diag-key-6")
            ))
        ).scalars().all() == []


# ---------------------------------------------------------------- 投递器

def test_dispatch_cycle_sends_and_worker_completes(client, make_user):
    make_user(username="taskuser7", password="taskuser7-pass-123456")
    headers = _auth_headers(client, "taskuser7", "taskuser7-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-7", duration=1).json()["task_id"]

    settings = client.app.state.settings
    celery_app = create_celery_app(settings)
    with _db(client) as session:  # 初始 outbox 刚创建尚不到期，显式置为到期
        session.execute(
            update(TaskOutbox).where(TaskOutbox.task_id == task_id).values(
                next_attempt_at=now_utc() - timedelta(seconds=1)
            )
        )
        session.commit()
    stats = run_dispatch_cycle(client.app.state.session_factory, settings, celery_app)
    assert stats == {"claimed": 1, "sent": 1, "failed": 0}

    with _db(client) as session:
        outbox = session.execute(
            select(TaskOutbox).where(TaskOutbox.task_id == task_id)
        ).scalar_one()
        assert outbox.status == "sent"
        assert outbox.attempts == 1
        assert outbox.delivery_token
        assert outbox.sent_at is not None

    broker = redis_lib.Redis(
        host=settings.redis_host, port=settings.redis_port,
        password=settings.redis_password.get_secret_value(), db=settings.broker_redis_db,
    )
    assert broker.llen("defectguard:defectguard") == 1  # 消息真实落入 broker 队列

    execute_task(task_id, settings)  # 模拟 Worker 消费真实消息
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "succeeded"
        assert task.result_json == {"duration_seconds": 1, "completed": True}
        assert task.execution_token is None
        version = task.version

    execute_task(task_id, settings)  # 重复消息：原子领取失败即安全结束
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "succeeded"
        assert task.version == version  # 不覆盖终态与进度


def test_dispatch_redis_unavailable_reaches_dead(client, make_user, monkeypatch):
    make_user(username="taskuser8", password="taskuser8-pass-123456")
    headers = _auth_headers(client, "taskuser8", "taskuser8-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-8", duration=1).json()["task_id"]

    # 投递到不可达 Redis（连接拒绝），尝试上限 3；退避延迟打平避免真实等待。
    bad_settings = load_settings(redis_port=16399, task_dispatch_attempts=3,
                                 task_loops_enabled=False, broker_redis_db=15)
    bad_celery = create_celery_app(bad_settings)
    monkeypatch.setattr(dispatcher_module, "delivery_delay_seconds", lambda attempts: 0)
    with _db(client) as session:  # 初始 outbox 刚创建尚不到期，显式置为到期
        session.execute(
            update(TaskOutbox).where(TaskOutbox.task_id == task_id).values(
                next_attempt_at=now_utc() - timedelta(seconds=1)
            )
        )
        session.commit()

    for expected_attempt in (1, 2):
        stats = run_dispatch_cycle(client.app.state.session_factory, bad_settings, bad_celery)
        assert stats == {"claimed": 1, "sent": 0, "failed": 1}
        with _db(client) as session:
            outbox = session.execute(
                select(TaskOutbox).where(TaskOutbox.task_id == task_id)
            ).scalar_one()
            assert outbox.status == "pending"
            assert outbox.attempts == expected_attempt
            assert outbox.lease_until is None
            assert outbox.delivery_token is None

    stats = run_dispatch_cycle(client.app.state.session_factory, bad_settings, bad_celery)
    assert stats == {"claimed": 1, "sent": 0, "failed": 1}
    with _db(client) as session:
        outbox = session.execute(
            select(TaskOutbox).where(TaskOutbox.task_id == task_id)
        ).scalar_one()
        assert outbox.status == "dead"  # 到上限
        task = session.get(AsyncTask, task_id)
        assert task.status == "failed"  # 仍 queued 的任务写明确失败
        assert task.error_code == "TASK_DISPATCH_FAILED"


# ---------------------------------------------------------------- 执行原语与恢复

def test_claim_heartbeat_and_success_writeback(client, make_user):
    make_user(username="taskuser9", password="taskuser9-pass-123456")
    headers = _auth_headers(client, "taskuser9", "taskuser9-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-9", duration=0).json()["task_id"]

    settings = client.app.state.settings
    token = "claim-token-9"
    with _db(client) as session:
        assert claim_task(session, settings, task_id, token)
        task = session.get(AsyncTask, task_id)
        assert task.status == "running"
        assert task.execution_token == token
        assert as_utc(task.lease_until) > now_utc()
        first_lease = task.lease_until
        time.sleep(0.01)  # 保证续租时间戳严格后移（DATETIME(6) 微秒粒度）
        assert heartbeat_task(session, settings, task_id, token)
        task = session.get(AsyncTask, task_id)
        assert as_utc(task.lease_until) > as_utc(first_lease)
        assert finish_task(session, task_id, token, succeeded=True,
                           result_json={"ok": True}, actor_id=task.actor_id) == "succeeded"
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "succeeded"
        assert task.result_json == {"ok": True}
        assert task.execution_token is None


def test_lease_expired_recovery_and_stale_token_writeback(client, make_user):
    make_user(username="taskuser10", password="taskuser10-pass-123456")
    headers = _auth_headers(client, "taskuser10", "taskuser10-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-10", duration=0).json()["task_id"]

    settings = client.app.state.settings
    token = "claim-token-10"
    with _db(client) as session:
        assert claim_task(session, settings, task_id, token)
        task = session.get(AsyncTask, task_id)
        actor_id = task.actor_id
        # 模拟 Worker 停止心跳直至租约过期
        session.execute(
            update(AsyncTask).where(AsyncTask.id == task_id).values(
                lease_until=now_utc() - timedelta(seconds=1)
            )
        )
        session.commit()

    stats = run_coordinator_cycle(client.app.state.session_factory, settings)
    assert stats["lease_expired"] == 1
    assert stats["recovered"] == 1
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "failed"
        assert task.error_code == "TASK_LEASE_EXPIRED"
        successor = session.execute(
            select(AsyncTask).where(AsyncTask.retry_of == task_id)
        ).scalar_one()
        assert successor.status == "queued"
        assert successor.root_task_id == task.root_task_id
        assert successor.retry_count == 1
        assert successor.payload == task.payload
        outbox = session.execute(
            select(TaskOutbox).where(TaskOutbox.task_id == successor.id)
        ).scalar_one()
        assert outbox.status == "pending"  # successor 与 outbox 同事务

    # 旧 Worker 恢复后用过期令牌写回：不得覆盖协调器终态
    with _db(client) as session:
        assert finish_task(session, task_id, token, succeeded=True,
                           result_json={"late": True}, actor_id=actor_id) is None
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "failed"
        assert task.result_json is None


def test_queue_timeout_marks_failed_and_recovers(client, make_user):
    make_user(username="taskuser11", password="taskuser11-pass-123456")
    headers = _auth_headers(client, "taskuser11", "taskuser11-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-11", duration=1).json()["task_id"]

    with _db(client) as session:
        session.execute(
            update(AsyncTask).where(AsyncTask.id == task_id).values(
                queued_deadline=now_utc() - timedelta(seconds=1)
            )
        )
        session.commit()

    stats = run_coordinator_cycle(client.app.state.session_factory, client.app.state.settings)
    assert stats["queue_timeout"] == 1
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "failed"
        assert task.error_code == "TASK_QUEUE_TIMEOUT"


def test_auto_recovery_chain_limit(client, make_user):
    make_user(username="taskuser12", password="taskuser12-pass-123456")
    headers = _auth_headers(client, "taskuser12", "taskuser12-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-12", duration=1).json()["task_id"]

    settings = load_settings(task_auto_recovery_limit=2, task_loops_enabled=False,
                             broker_redis_db=15)
    with _db(client) as session:
        session.execute(
            update(AsyncTask).where(AsyncTask.id == task_id).values(
                status="failed", error_code="TASK_DISPATCH_FAILED", error_message="投递失败"
            )
        )
        session.commit()

    # 第 1 次恢复
    run_coordinator_cycle(client.app.state.session_factory, settings)
    with _db(client) as session:
        first = session.execute(
            select(AsyncTask).where(AsyncTask.retry_of == task_id)
        ).scalar_one()
        assert first.retry_count == 1
        session.execute(
            update(AsyncTask).where(AsyncTask.id == first.id).values(
                status="failed", error_code="TASK_DISPATCH_FAILED"
            )
        )
        session.commit()
    # 第 2 次恢复（达到上限）
    run_coordinator_cycle(client.app.state.session_factory, settings)
    with _db(client) as session:
        second = session.execute(
            select(AsyncTask).where(AsyncTask.retry_of == first.id)
        ).scalar_one()
        assert second.retry_count == 2
        session.execute(
            update(AsyncTask).where(AsyncTask.id == second.id).values(
                status="failed", error_code="TASK_DISPATCH_FAILED"
            )
        )
        session.commit()
    # 超限：不再自动恢复
    stats = run_coordinator_cycle(client.app.state.session_factory, settings)
    assert stats["recovered"] == 0
    with _db(client) as session:
        third = session.execute(
            select(AsyncTask).where(AsyncTask.retry_of == second.id)
        ).scalar_one_or_none()
        assert third is None


# ---------------------------------------------------------------- A29/A30

def test_list_tasks_contract_and_filters(client, make_user):
    make_user(username="taskuser13", password="taskuser13-pass-123456")
    headers = _auth_headers(client, "taskuser13", "taskuser13-pass-123456")
    ids = []
    for index in range(3):
        ids.append(
            _create_diagnostics(client, headers, key=f"diag-list-{index}",
                                duration=1).json()["task_id"]
        )
        time.sleep(0.01)  # created_at 取不同时间戳便于顺序断言

    response = client.get(TASKS_URL, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert [item["id"] for item in body["items"]] == list(reversed(ids))  # created_at desc
    first = body["items"][0]
    assert set(first) == {
        "id", "type", "status", "stage", "progress", "processed", "total",
        "heartbeat_at", "created_at", "started_at", "finished_at", "health",
        "retry_of", "result", "error",
    }
    assert first["type"] == "diagnostics"
    assert first["status"] == "queued"
    assert first["health"] == "healthy"

    by_type = client.get(TASKS_URL, params={"type": "diagnostics"}, headers=headers).json()
    assert by_type["total"] == 3
    by_status = client.get(TASKS_URL, params={"status": "succeeded"}, headers=headers).json()
    assert by_status["total"] == 0
    paged = client.get(TASKS_URL, params={"page": 2, "page_size": 2}, headers=headers).json()
    assert paged["total"] == 3
    assert len(paged["items"]) == 1
    assert paged["page"] == 2

    assert client.get(TASKS_URL, params={"status": "exploding"}, headers=headers).status_code == 422
    assert client.get(TASKS_URL, params={"page": 0}, headers=headers).status_code == 422
    assert client.get(TASKS_URL, params={"page_size": 101}, headers=headers).status_code == 422


def test_get_task_detail_fields_and_404(client, make_user):
    make_user(username="taskuser14", password="taskuser14-pass-123456")
    headers = _auth_headers(client, "taskuser14", "taskuser14-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-14", duration=1).json()["task_id"]

    detail = client.get(f"{TASKS_URL}/{task_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == task_id
    assert "execution_token" not in body  # 内部 token 不入响应
    assert "payload_hash" not in body
    assert body["error"] is None
    assert body["retry_of"] is None
    assert body["created_at"].endswith("Z")

    missing = client.get(f"{TASKS_URL}/00000000-0000-0000-0000-000000000000", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "TASK_NOT_FOUND"

    # 失败任务的 error 结构与 stale 派生
    with _db(client) as session:
        session.execute(
            update(AsyncTask).where(AsyncTask.id == task_id).values(
                status="failed", error_code="TASK_EXECUTION_FAILED",
                error_message="安全化消息", finished_at=now_utc(),
            )
        )
        session.commit()
    detail = client.get(f"{TASKS_URL}/{task_id}", headers=headers).json()
    assert detail["error"] == {
        "code": "TASK_EXECUTION_FAILED",
        "message": "安全化消息",
        "request_id": detail["error"]["request_id"],
    }

    with _db(client) as session:
        session.execute(
            update(AsyncTask).where(AsyncTask.id == task_id).values(
                status="running", lease_until=now_utc() - timedelta(seconds=1)
            )
        )
        session.commit()
    detail = client.get(f"{TASKS_URL}/{task_id}", headers=headers).json()
    assert detail["health"] == "stale"  # running 且租约过期


# ---------------------------------------------------------------- A32 取消

def test_cancel_queued_task_idempotent(client, make_user):
    make_user(username="taskuser15", password="taskuser15-pass-123456")
    headers = _auth_headers(client, "taskuser15", "taskuser15-pass-123456")
    task_id = _create_diagnostics(client, headers, key="diag-key-15", duration=1).json()["task_id"]

    first = client.post(f"{TASKS_URL}/{task_id}/cancel", headers=headers)
    assert first.status_code == 204
    assert first.content == b""
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "cancelled"
        assert task.finished_at is not None

    replay = client.post(f"{TASKS_URL}/{task_id}/cancel", headers=headers)
    assert replay.status_code == 204

    # 后到消息不能领取：投递器对非 queued 任务直接终结 outbox，不发消息
    settings = client.app.state.settings
    stats = run_dispatch_cycle(
        client.app.state.session_factory, settings, create_celery_app(settings)
    )
    assert stats == {"claimed": 1, "sent": 0, "failed": 0}
    with _db(client) as session:
        outbox = session.execute(
            select(TaskOutbox).where(TaskOutbox.task_id == task_id)
        ).scalar_one()
        assert outbox.status == "dead"
        assert outbox.last_error_code == "TASK_NOT_QUEUED"
        task = session.get(AsyncTask, task_id)
        assert task.status == "cancelled"  # 任务终态不被投递器改写


def test_cancel_running_checkpoint_and_completion_race(client, make_user):
    make_user(username="taskuser16", password="taskuser16-pass-123456")
    headers = _auth_headers(client, "taskuser16", "taskuser16-pass-123456")
    settings = client.app.state.settings

    # 场景 1：cancel_requested 先提交 → 成功写回必须失败并走取消检查点
    task_id = _create_diagnostics(client, headers, key="diag-key-16a", duration=1).json()["task_id"]
    token = "claim-token-16a"
    with _db(client) as session:
        assert claim_task(session, settings, task_id, token)
        actor_id = session.get(AsyncTask, task_id).actor_id
    response = client.post(f"{TASKS_URL}/{task_id}/cancel", headers=headers)
    assert response.status_code == 202
    assert response.json() == {"task_id": task_id, "status": "cancel_requested"}
    with _db(client) as session:
        assert finish_task(session, task_id, token, succeeded=True,
                           result_json={"late": True}, actor_id=actor_id) == "cancelled"
    with _db(client) as session:
        task = session.get(AsyncTask, task_id)
        assert task.status == "cancelled"
        assert task.result_json is None  # 不能写 succeeded

    # 场景 2：完成先提交 → 取消返回 409
    task_id = _create_diagnostics(client, headers, key="diag-key-16b", duration=1).json()["task_id"]
    token = "claim-token-16b"
    with _db(client) as session:
        assert claim_task(session, settings, task_id, token)
        actor_id = session.get(AsyncTask, task_id).actor_id
        assert finish_task(session, task_id, token, succeeded=True,
                           result_json={"ok": True}, actor_id=actor_id) == "succeeded"
    response = client.post(f"{TASKS_URL}/{task_id}/cancel", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "TASK_STATE_CONFLICT"


# ---------------------------------------------------------------- A31 重试

def test_retry_contract_replay_and_conflicts(client, make_user):
    make_user(username="taskuser17", password="taskuser17-pass-123456")
    headers = _auth_headers(client, "taskuser17", "taskuser17-pass-123456")
    settings = client.app.state.settings
    task_id = _create_diagnostics(client, headers, key="diag-key-17", duration=1).json()["task_id"]

    def fail_task():
        with _db(client) as session:
            token = "claim-token-17"
            assert claim_task(session, settings, task_id, token)
            actor_id = session.get(AsyncTask, task_id).actor_id
            assert finish_task(session, task_id, token, succeeded=False,
                               error_code="TASK_EXECUTION_FAILED",
                               error_message="模拟业务失败", actor_id=actor_id) == "failed"

    # 非终态不可重试
    response = client.post(
        f"{TASKS_URL}/{task_id}/retry", headers={**headers, "Idempotency-Key": "retry-k1"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "TASK_STATE_CONFLICT"

    fail_task()
    response = client.post(
        f"{TASKS_URL}/{task_id}/retry", headers={**headers, "Idempotency-Key": "retry-k1"}
    )
    assert response.status_code == 202
    successor_id = response.json()["task_id"]
    assert response.json()["retry_of"] == task_id
    assert response.json()["status"] == "queued"
    with _db(client) as session:
        successor = session.get(AsyncTask, successor_id)
        assert successor.retry_count == 1
        assert successor.root_task_id == session.get(AsyncTask, task_id).root_task_id
        assert successor.status == "queued"
        outbox = session.execute(
            select(TaskOutbox).where(TaskOutbox.task_id == successor_id)
        ).scalar_one()
        assert outbox.status == "pending"

    # 同幂等键重放返回原 successor
    replay = client.post(
        f"{TASKS_URL}/{task_id}/retry", headers={**headers, "Idempotency-Key": "retry-k1"}
    )
    assert replay.status_code == 202
    assert replay.json()["task_id"] == successor_id

    # 不同幂等键且已有 successor → 409 TASK_RETRY_EXISTS
    conflict = client.post(
        f"{TASKS_URL}/{task_id}/retry", headers={**headers, "Idempotency-Key": "retry-k2"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "TASK_RETRY_EXISTS"


def test_concurrent_retry_single_successor(client, make_user):
    make_user(username="taskuser18", password="taskuser18-pass-123456")
    headers = _auth_headers(client, "taskuser18", "taskuser18-pass-123456")
    settings = client.app.state.settings
    task_id = _create_diagnostics(client, headers, key="diag-key-18", duration=1).json()["task_id"]
    with _db(client) as session:
        assert claim_task(session, settings, task_id, "claim-token-18")
        actor_id = session.get(AsyncTask, task_id).actor_id
        assert finish_task(session, task_id, "claim-token-18", succeeded=False,
                           error_code="TASK_EXECUTION_FAILED",
                           error_message="模拟业务失败", actor_id=actor_id) == "failed"

    def attempt(key):
        return client.post(
            f"{TASKS_URL}/{task_id}/retry", headers={**headers, "Idempotency-Key": key}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["retry-c1", "retry-c2"]))
    assert sorted(results) == [202, 409]  # 并发下原任务最多一个直接 successor

    with _db(client) as session:
        successors = session.execute(
            select(AsyncTask).where(AsyncTask.retry_of == task_id)
        ).scalars().all()
        assert len(successors) == 1


# ---------------------------------------------------------------- SEC-RBAC

def test_rbac_viewer_member_admin(client, make_user):
    make_user(username="taskviewer", password="taskviewer-pass-123456", role="Viewer")
    make_user(username="taskmember", password="taskmember-pass-123456", role="Member")
    make_user(username="taskadmin", password="taskadmin-pass-123456", role="Admin")
    viewer_headers = _auth_headers(client, "taskviewer", "taskviewer-pass-123456")
    member_headers = _auth_headers(client, "taskmember", "taskmember-pass-123456")
    admin_headers = _auth_headers(client, "taskadmin", "taskadmin-pass-123456")

    # Viewer 不能发起任务
    response = client.post(
        DIAGNOSTICS_URL, json={"duration_seconds": 1},
        headers={**viewer_headers, "Idempotency-Key": "rbac-v1"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_FORBIDDEN"

    member_task = _create_diagnostics(client, member_headers, key="rbac-m1", duration=1)
    assert member_task.status_code == 202
    task_id = member_task.json()["task_id"]

    # Viewer 可查询共享任务，但不能取消/重试
    assert client.get(TASKS_URL, headers=viewer_headers).status_code == 200
    assert client.get(f"{TASKS_URL}/{task_id}", headers=viewer_headers).status_code == 200
    cancel = client.post(f"{TASKS_URL}/{task_id}/cancel", headers=viewer_headers)
    assert cancel.status_code == 403
    retry = client.post(
        f"{TASKS_URL}/{task_id}/retry", headers={**viewer_headers, "Idempotency-Key": "rbac-v2"}
    )
    assert retry.status_code == 403

    # Member 不能操作他人任务
    other_task = _create_diagnostics(client, admin_headers, key="rbac-a1", duration=1)
    other_task = other_task.json()["task_id"]
    member_cancel = client.post(f"{TASKS_URL}/{other_task}/cancel", headers=member_headers)
    assert member_cancel.status_code == 403
    assert client.post(
        f"{TASKS_URL}/{other_task}/retry",
        headers={**member_headers, "Idempotency-Key": "rbac-m2"},
    ).status_code == 403

    # Admin 可操作任意任务
    assert client.post(f"{TASKS_URL}/{task_id}/cancel", headers=admin_headers).status_code == 204
    with _db(client) as session:
        assert session.get(AsyncTask, task_id).status == "cancelled"

    # 操作者本身可操作自己的任务
    own = _create_diagnostics(client, member_headers, key="rbac-m3", duration=1).json()["task_id"]
    assert client.post(f"{TASKS_URL}/{own}/cancel", headers=member_headers).status_code == 204
