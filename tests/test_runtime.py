"""Explicit platform acceptance against dedicated defectguard-wang containers."""

import os
import subprocess
import time
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from packages.platform.config import load_settings
from packages.platform.connections import Connections

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1", reason="explicit opt-in required"
    ),
]


def docker(*args):
    command = [os.environ["DG_DOCKER_EXE"], "compose", "--profile", "full", *args]
    result = subprocess.run(command, capture_output=True, timeout=180, check=False)
    assert result.returncode == 0, (
        "Project Docker action failed; inspect local logs without secrets"
    )


def wait_ready():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if (
                httpx.get(
                    "http://127.0.0.1:18080/api/v1/health/ready", timeout=8, trust_env=False
                ).status_code
                == 200
            ):
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    pytest.fail("Readiness did not recover")


def _member_session():
    # 预置 Member 账号登录，返回 (client, headers)；Origin 与部署允许列表一致。
    settings = load_settings()
    client = httpx.Client(base_url="http://127.0.0.1:18080", timeout=8, trust_env=False)
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": settings.provision_username,
            "password": settings.provision_password.get_secret_value(),
        },
        headers={"Origin": settings.frontend_origin},
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    return client, headers


def _create_diagnostics(client, headers, *, key, duration):
    response = client.post(
        "/api/v1/tasks/diagnostics",
        json={"duration_seconds": duration},
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def _poll_task(client, headers, task_id, *, statuses, timeout=180):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in statuses:
            return last
        time.sleep(2)
    pytest.fail(f"task {task_id} did not reach {statuses}; last={last}")


def _restore_worker():
    docker("up", "-d", "--wait", "--wait-timeout", "120", "worker")
    wait_ready()


def test_web_proxy_and_worker():
    with httpx.Client(base_url="http://127.0.0.1:18080", timeout=8, trust_env=False) as client:
        page = client.get("/")
        assert page.status_code == 200 and '<div id="app">' in page.text
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        UUID(response.headers["X-Request-ID"])
        response = client.get("/api/v1/no-such-resource")
        assert response.status_code == 404 and response.json()["code"] == "SYSTEM_NOT_FOUND"
    docker("exec", "-T", "worker", "python", "-m", "apps.worker.health")


def test_live_auth_roundtrip():
    # 容器化认证验收：登录/访问/刷新轮转/CSRF 拒绝/退出撤销（真实 HTTP 栈）。
    settings = load_settings()
    with httpx.Client(base_url="http://127.0.0.1:18080", timeout=8, trust_env=False) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "username": settings.provision_username,
                "password": settings.provision_password.get_secret_value(),
            },
            headers={"Origin": settings.frontend_origin},
        )
        assert response.status_code == 201
        body = response.json()
        access = body["access_token"]
        csrf = body["csrf_token"]
        me = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {access}"})
        assert me.status_code == 200 and me.json()["username"] == settings.provision_username
        rotated = client.post(
            "/api/v1/auth/refresh",
            headers={"X-CSRF-Token": csrf, "Origin": settings.frontend_origin},
        )
        assert rotated.status_code == 200
        new_csrf = rotated.json()["csrf_token"]
        stale = client.post(
            "/api/v1/auth/refresh",
            headers={"X-CSRF-Token": csrf, "Origin": settings.frontend_origin},
        )
        assert stale.status_code == 403 and stale.json()["code"] == "AUTH_CSRF_REJECTED"
        logout = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": new_csrf, "Origin": settings.frontend_origin},
        )
        assert logout.status_code == 204
        assert "max-age=0" in logout.headers.get("set-cookie", "").lower()
        after = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {access}"})
        assert after.status_code == 401 and after.json()["code"] == "AUTH_REQUIRED"


def test_dependency_failure_and_restart_persistence():
    marker = str(uuid4())
    connections = Connections(load_settings())
    try:
        with connections.engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE IF NOT EXISTS platform_volume_probe (id CHAR(36) PRIMARY KEY)")
            )
            connection.execute(
                text("INSERT INTO platform_volume_probe(id) VALUES (:id)"), {"id": marker}
            )
        connections.redis.set("defectguard:volume-probe", marker)
    finally:
        connections.close()
    try:
        docker("stop", "redis")
        response = httpx.get(
            "http://127.0.0.1:18080/api/v1/health/ready", timeout=8, trust_env=False
        )
        assert response.status_code == 503
        assert response.json()["code"] == "SYSTEM_DEPENDENCY_UNAVAILABLE"
        UUID(response.json()["request_id"])
        assert (
            httpx.get(
                "http://127.0.0.1:18080/api/v1/health", timeout=8, trust_env=False
            ).status_code
            == 200
        )
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "redis")
    wait_ready()
    docker("restart", "mysql", "redis")
    wait_ready()
    connections = Connections(load_settings())
    try:
        with connections.engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT id FROM platform_volume_probe WHERE id=:id"), {"id": marker}
                ).scalar()
                == marker
            )
        assert connections.redis.get("defectguard:volume-probe") == marker.encode()
    finally:
        connections.close()


def test_live_task_roundtrip():
    # INT-OUTBOX 实链路：创建 → 投递器 → 真实 Worker → 查询；同键重放返回原任务。
    client, headers = _member_session()
    key = f"runtime-roundtrip-{uuid4()}"
    task_id = _create_diagnostics(client, headers, key=key, duration=1)
    task = _poll_task(client, headers, task_id, statuses=("succeeded", "failed"), timeout=120)
    assert task["status"] == "succeeded", task
    assert task["result"] == {"duration_seconds": 1, "completed": True}
    assert task["error"] is None
    assert task["stage"] == "done"
    assert task["progress"] == 100

    replay = client.post(
        "/api/v1/tasks/diagnostics",
        json={"duration_seconds": 1},
        headers={**headers, "Idempotency-Key": key},
    )
    assert replay.status_code == 202
    assert replay.json()["task_id"] == task_id

    listing = client.get("/api/v1/tasks", headers=headers)
    assert listing.status_code == 200
    assert any(item["id"] == task_id for item in listing.json()["items"])

    # 终态 succeeded 上取消 → 409 TASK_STATE_CONFLICT。
    conflict = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "TASK_STATE_CONFLICT"
    client.close()


def test_rel_lease_kill_worker_recovers_via_successor():
    # REL-LEASE：kill Worker 后租约过期，协调器标记 failed 并按上限创建 successor；
    # Worker 恢复后 successor 真实执行成功。
    client, headers = _member_session()
    key = f"runtime-kill-{uuid4()}"
    task_id = _create_diagnostics(client, headers, key=key, duration=30)
    _poll_task(client, headers, task_id, statuses=("running",), timeout=60)
    try:
        docker("stop", "worker")
        failed = _poll_task(
            client, headers, task_id, statuses=("failed",), timeout=120
        )
        assert failed["error"]["code"] == "TASK_LEASE_EXPIRED", failed
    finally:
        _restore_worker()
    # 协调器标记 failed 的同时（同事务）创建 successor；Worker 恢复后执行成功。
    deadline = time.monotonic() + 60
    successor_id = None
    while time.monotonic() < deadline:
        listing = client.get("/api/v1/tasks", headers=headers).json()
        matches = [item["id"] for item in listing["items"] if item["retry_of"] == task_id]
        if matches:
            successor_id = matches[0]
            break
        time.sleep(2)
    assert successor_id, "coordinator did not create a successor"
    successor_detail = _poll_task(
        client, headers, successor_id, statuses=("succeeded", "failed"), timeout=180
    )
    assert successor_detail["status"] == "succeeded", successor_detail
    assert successor_detail["result"] == {"duration_seconds": 30, "completed": True}
    client.close()


def test_rel_cancel_running_checkpoint():
    # REL-CANCEL：running 取消 202 → Worker 取消检查点写 cancelled → 终态冲突 409。
    client, headers = _member_session()
    key = f"runtime-cancel-{uuid4()}"
    task_id = _create_diagnostics(client, headers, key=key, duration=30)
    _poll_task(client, headers, task_id, statuses=("running",), timeout=60)
    response = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 202, response.text
    assert response.json() == {"task_id": task_id, "status": "cancel_requested"}
    task = _poll_task(client, headers, task_id, statuses=("cancelled",), timeout=60)
    assert task["result"] is None
    # 已取消任务重复取消幂等 204；409 冲突分支由 roundtrip 测试在 succeeded 上覆盖。
    repeat = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert repeat.status_code == 204, repeat.text
    client.close()


def test_rel_dispatch_retries_through_redis_outage():
    # INT-OUTBOX 故障注入：先停 Redis 再创建任务，保证首轮投递必然失败；
    # 投递器退避重试，恢复后送达并真实执行成功。
    client, headers = _member_session()
    key = f"runtime-redis-{uuid4()}"
    try:
        docker("stop", "redis")
        task_id = _create_diagnostics(client, headers, key=key, duration=1)
        time.sleep(15)  # 观察期内投递失败，走 pending 退避
    finally:
        docker("up", "-d", "--wait", "--wait-timeout", "120", "redis")
    wait_ready()
    task = _poll_task(client, headers, task_id, statuses=("succeeded", "failed"), timeout=240)
    assert task["status"] == "succeeded", task  # 退避重试最终送达，未悬挂为 queued
    assert task["result"] == {"duration_seconds": 1, "completed": True}
    client.close()


def test_missing_secrets_reject_real_process_startup():
    import sys

    environment = {key: value for key, value in os.environ.items() if not key.startswith("DG_")}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from packages.platform.config import load_settings; "
            "from apps.api.application import create_app; "
            "create_app(load_settings(_env_file='.env.example'))",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "SYSTEM_CONFIGURATION_INVALID" in result.stderr
    assert "signing_key" in result.stderr
