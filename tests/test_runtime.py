"""Explicit platform acceptance against dedicated defectguard-wang containers."""

import json
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
    return result.stdout.decode().strip()


def raw_docker(*args):
    result = subprocess.run(
        [os.environ["DG_DOCKER_EXE"], *args], capture_output=True, timeout=180, check=False
    )
    if result.returncode != 0:
        # Classify known environment failures without dumping commands, stdout or secrets.
        diagnostic = result.stderr.decode(errors="replace").lower()
        category = "unknown"
        if "user configured subnets" in diagnostic:
            category = "static_ip_requires_explicit_subnet"
        elif "address already in use" in diagnostic:
            category = "address_already_in_use"
        pytest.fail("Scoped Docker DNS acceptance action failed: " + category)
    return result.stdout.decode().strip()


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


def test_proxy_recovers_after_api_address_changes():
    wait_ready()
    docker("exec", "-T", "web", "nginx", "-t")
    web_id = docker("ps", "-q", "web")
    api_id = docker("ps", "-q", "api")
    networks = json.loads(
        raw_docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", api_id)
    )
    assert len(networks) == 1
    network, details = next(iter(networks.items()))
    assert network.startswith("defectguard-wang_")
    old_address = details["IPAddress"]
    image = docker("images", "-q", "redis")
    blockers = []
    # Some engines prohibit static addresses on an automatically allocated subnet.
    # Dynamic blockers exercise the allocator without changing the project network.
    # Existing Redis image, no new pull, ports, mounts, application secrets or volumes.
    try:
        for _ in range(3):
            docker("rm", "--stop", "--force", "api")
            blocker = "dg-stage3-dns-" + uuid4().hex
            raw_docker(
                "run",
                "--pull",
                "never",
                "-d",
                "--name",
                blocker,
                "--network",
                network,
                image,
                "sleep",
                "300",
            )
            blockers.append(blocker)
            docker("up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", "api")
            new_api_id = docker("ps", "-q", "api")
            updated = json.loads(
                raw_docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", new_api_id)
            )
            if updated[network]["IPAddress"] != old_address:
                break
        assert updated[network]["IPAddress"] != old_address
        assert docker("ps", "-q", "web") == web_id
        wait_ready()
    finally:
        for blocker in blockers:
            raw_docker("rm", "-f", blocker)
        docker("up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", "api")


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
