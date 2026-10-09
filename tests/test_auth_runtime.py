"""Opt-in HTTP authentication acceptance using the explicit local administrator."""

import os

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.auth.bootstrap import BootstrapSettings
from packages.auth.security import digest
from packages.persistence.models import AuthSession, OperationLog
from packages.platform.connections import Connections

pytestmark = [
    pytest.mark.runtime,
    pytest.mark.skipif(
        os.environ.get("DG_RUN_RUNTIME_TESTS") != "1", reason="explicit local opt-in required"
    ),
]


def test_authentication_over_real_web_proxy():
    settings = BootstrapSettings()
    assert settings.environment == "development" and settings.mysql_host == "127.0.0.1"
    origin = "http://127.0.0.1:18080"
    with httpx.Client(base_url=origin, timeout=8, trust_env=False) as api:
        result = api.post(
            "/api/v1/auth/login",
            json={
                "username": settings.bootstrap_username,
                "password": settings.bootstrap_password.get_secret_value(),
            },
            headers={"Origin": origin},
        )
        assert result.status_code == 201
        assert result.headers["Cache-Control"] == "no-store"
        initial_refresh = api.cookies["dg_refresh"]
        authorization = {"Authorization": "Bearer " + result.json()["access_token"]}
        me = api.get("/api/v1/users/me", headers=authorization)
        assert me.status_code == 200
        assert me.json()["username"] == settings.bootstrap_username and me.json()["role"] == "Admin"
        updated = api.post(
            "/api/v1/auth/refresh",
            headers={"Origin": origin, "X-CSRF-Token": result.json()["csrf_token"]},
        )
        assert updated.status_code == 200 and api.cookies["dg_refresh"] != initial_refresh
        refresh_hash = digest(api.cookies["dg_refresh"])
        assert (
            api.post(
                "/api/v1/auth/logout",
                headers={"Origin": origin, "X-CSRF-Token": updated.json()["csrf_token"]},
            ).status_code
            == 204
        )
        assert "dg_refresh" not in api.cookies
        assert api.get("/api/v1/users/me", headers=authorization).status_code == 401
    connections = Connections(settings)
    try:
        with Session(connections.engine) as db:
            session = db.scalar(select(AuthSession).where(AuthSession.refresh_hash == refresh_hash))
            assert session.revoked_at is not None and session.generation == 1
            logs = db.scalars(
                select(OperationLog)
                .where(OperationLog.object_id == session.id)
                .order_by(OperationLog.created_at)
            ).all()
            assert [row.action for row in logs] == ["auth.login", "auth.refresh", "auth.logout"]
            assert all(row.detail_json == {} for row in logs)
    finally:
        connections.close()
