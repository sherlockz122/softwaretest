import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from apps.api.application import create_app
from apps.api.auth import current_user
from packages.auth.bootstrap import BootstrapSettings, seed
from packages.auth.security import (
    PASSWORD_HASHER,
    AuthError,
    authorize_task_owner,
    digest,
    require_role,
)
from packages.auth.service import now
from packages.persistence.models import AuthSession, OperationLog, User
from packages.platform.config import ConfigurationError
from packages.platform.connections import Connections
from tests.auth_support import migrate, scratch_database

pytestmark = pytest.mark.integration
PASSWORD = "test-fixture-password-only"


@pytest.fixture
def auth_env():
    with scratch_database() as (settings, engine):
        migrate(engine)
        with Session(engine) as db, db.begin():
            users = []
            for role in ("Admin", "Member", "Viewer"):
                user = User(
                    id=str(uuid4()),
                    username=role.lower(),
                    role=role,
                    is_active=True,
                    password_hash=PASSWORD_HASHER.hash(PASSWORD),
                )
                db.add(user)
                users.append(user.id)
        yield settings, engine, users


def client(settings):
    return TestClient(create_app(settings), base_url="http://127.0.0.1")


def login(api, name="member", password=PASSWORD):
    result = api.post("/api/v1/auth/login", json={"username": name, "password": password})
    assert result.status_code == 201, result.json().get("code")
    return result


def auth_header(result):
    return {"Authorization": "Bearer " + result.json()["access_token"]}


def test_login_rotation_logout_and_audit(auth_env):
    settings, engine, users = auth_env
    with client(settings) as api:
        result = login(api, "MEMBER")
        assert result.headers["Cache-Control"] == "no-store"
        cookie = result.headers["Set-Cookie"]
        assert (
            "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/v1/auth" in cookie
        )
        initial_refresh = api.cookies["dg_refresh"]
        csrf = result.json()["csrf_token"]
        assert api.get("/api/v1/users/me", headers=auth_header(result)).json() == {
            "id": users[1],
            "username": "member",
            "role": "Member",
        }
        with Session(engine) as db:
            row = db.scalar(select(AuthSession))
            expires = row.expires_at
            assert row.refresh_hash == digest(initial_refresh)
            assert row.csrf_hash == digest(csrf)
            assert row.generation == 0
        updated = api.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert updated.status_code == 200
        assert api.cookies["dg_refresh"] != initial_refresh
        assert updated.json()["csrf_token"] != csrf
        with Session(engine) as db:
            row = db.scalar(select(AuthSession))
            assert row.generation == 1 and row.expires_at == expires
        stale = api.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": "dg_refresh=" + initial_refresh, "X-CSRF-Token": csrf},
        )
        assert stale.status_code == 401
        refresh_value = api.cookies["dg_refresh"]
        headers = {"X-CSRF-Token": updated.json()["csrf_token"]}
        assert api.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert api.get("/api/v1/users/me", headers=auth_header(result)).status_code == 401
        assert api.get("/api/v1/users/me", headers=auth_header(updated)).status_code == 401
        assert (
            api.post(
                "/api/v1/auth/logout", headers={**headers, "Cookie": "dg_refresh=" + refresh_value}
            ).status_code
            == 204
        )
        with Session(engine) as db:
            logs = db.scalars(select(OperationLog).order_by(OperationLog.created_at)).all()
            assert [row.action for row in logs] == ["auth.login", "auth.refresh", "auth.logout"]
            assert all(row.actor_id == users[1] and row.detail_json == {} for row in logs)
            assert initial_refresh not in json.dumps([row.detail_json for row in logs])


def test_csrf_origin_content_type_and_sensitive_input(auth_env):
    settings, engine, users = auth_env
    with client(settings) as api:
        result = login(api)
        csrf = result.json()["csrf_token"]
        for path in ("refresh", "logout"):
            assert api.post("/api/v1/auth/" + path).status_code == 403
            assert (
                api.post("/api/v1/auth/" + path, headers={"X-CSRF-Token": "wrong"}).status_code
                == 403
            )
            assert (
                api.post(
                    "/api/v1/auth/" + path,
                    headers={"X-CSRF-Token": csrf, "Origin": "http://hostile.example"},
                ).status_code
                == 403
            )
        denied = api.post(
            "/api/v1/auth/login",
            json={"username": "member", "password": PASSWORD},
            headers={"Origin": "http://hostile.example"},
        )
        assert denied.status_code == 403
        assert (
            api.post(
                "/api/v1/auth/login",
                content=json.dumps({"username": "member", "password": PASSWORD}),
                headers={"Content-Type": "text/plain"},
            ).status_code
            == 415
        )
        invalid = api.post(
            "/api/v1/auth/login",
            json={"username": "member", "password": PASSWORD, "private_secret": "must-not-leak"},
        )
        assert (
            invalid.status_code == 422
            and "must-not-leak" not in invalid.text
            and PASSWORD not in invalid.text
        )
        assert (
            api.post(
                "/api/v1/auth/refresh",
                headers={"X-CSRF-Token": csrf, "Origin": settings.frontend_origin},
            ).status_code
            == 200
        )


def test_wrong_disabled_expired_and_tampered(auth_env):
    settings, engine, users = auth_env
    with client(settings) as api:
        for name in ("absent", "member"):
            denied = api.post("/api/v1/auth/login", json={"username": name, "password": "wrong"})
            assert denied.status_code == 401 and denied.json()["code"] == "AUTH_INVALID_CREDENTIALS"
        result = login(api)
        with Session(engine) as db, db.begin():
            db.get(User, users[1]).is_active = False
        assert api.get("/api/v1/users/me", headers=auth_header(result)).status_code == 401
        assert (
            api.post(
                "/api/v1/auth/refresh", headers={"X-CSRF-Token": result.json()["csrf_token"]}
            ).status_code
            == 401
        )
        denied = api.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD})
        assert denied.json()["code"] == "AUTH_INVALID_CREDENTIALS"
        with Session(engine) as db, db.begin():
            db.get(User, users[1]).is_active = True
        result = login(api)
        for token in ("bad-token", result.json()["access_token"] + "tampered"):
            assert (
                api.get(
                    "/api/v1/users/me", headers={"Authorization": "Bearer " + token}
                ).status_code
                == 401
            )
        claims = jwt.decode(result.json()["access_token"], options={"verify_signature": False})
        invalid_claims = [
            {key: value for key, value in claims.items() if key != missing}
            for missing in ("sub", "sid", "iat", "exp", "iss", "aud")
        ]
        for payload in invalid_claims:
            token = jwt.encode(payload, settings.signing_key.get_secret_value(), algorithm="HS256")
            assert (
                api.get(
                    "/api/v1/users/me", headers={"Authorization": "Bearer " + token}
                ).status_code
                == 401
            )
        wrong_algorithm = jwt.encode(
            claims, settings.signing_key.get_secret_value(), algorithm="HS384"
        )
        assert (
            api.get(
                "/api/v1/users/me", headers={"Authorization": "Bearer " + wrong_algorithm}
            ).status_code
            == 401
        )
        for replacement in ({"exp": 0}, {"aud": "wrong"}, {"iss": "wrong"}):
            token = jwt.encode(
                {**claims, **replacement},
                settings.signing_key.get_secret_value(),
                algorithm="HS256",
            )
            assert (
                api.get(
                    "/api/v1/users/me", headers={"Authorization": "Bearer " + token}
                ).status_code
                == 401
            )
        with Session(engine) as db, db.begin():
            db.scalar(
                select(AuthSession).where(
                    AuthSession.refresh_hash == digest(api.cookies["dg_refresh"])
                )
            ).expires_at = now() - timedelta(seconds=1)
        assert api.get("/api/v1/users/me", headers=auth_header(result)).status_code == 401
        assert (
            api.post(
                "/api/v1/auth/refresh", headers={"X-CSRF-Token": result.json()["csrf_token"]}
            ).status_code
            == 401
        )


def test_concurrent_refresh_single_winner(auth_env):
    settings, engine, users = auth_env
    with client(settings) as api:
        result = login(api)
        headers = {
            "Cookie": "dg_refresh=" + api.cookies["dg_refresh"],
            "X-CSRF-Token": result.json()["csrf_token"],
        }

    def refresh_once(_):
        with client(settings) as api:
            return api.post("/api/v1/auth/refresh", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(refresh_once, range(2))) == [200, 401]
    with Session(engine) as db:
        assert db.scalar(select(AuthSession)).generation == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.action == "auth.refresh")
            )
            == 1
        )


def test_shared_rate_limit_and_redis_failure(auth_env, monkeypatch):
    settings, engine, users = auth_env
    limited = settings.model_copy(update={"login_rate_limit": 5})
    with client(limited) as api:
        for _ in range(5):
            assert (
                api.post(
                    "/api/v1/auth/login", json={"username": "absent", "password": "wrong"}
                ).status_code
                == 401
            )
    with client(limited) as other_process:
        response = other_process.post(
            "/api/v1/auth/login", json={"username": "absent", "password": "wrong"}
        )
        assert response.status_code == 429 and response.headers["Retry-After"] == "60"
    from redis.exceptions import ConnectionError

    def unavailable(*args, **kwargs):
        raise ConnectionError("must-not-leak")

    with client(settings) as api:
        monkeypatch.setattr(api.app.state.connections.redis, "eval", unavailable)
        result = api.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD})
        assert result.status_code == 503 and "must-not-leak" not in result.text


def test_roles_current_state_and_owner_helpers(auth_env):
    settings, engine, users = auth_env
    app = create_app(settings)

    @app.get("/test/member-only")
    def member_only(user=Depends(current_user)):
        return asdict(require_role(user, "Member"))

    with TestClient(app, base_url="http://127.0.0.1") as api:
        viewer = login(api, "viewer")
        assert api.get("/test/member-only", headers=auth_header(viewer)).status_code == 403
        member = login(api)
        assert api.get("/test/member-only", headers=auth_header(member)).status_code == 200
        from packages.auth.security import CurrentUser

        actor = CurrentUser(users[1], "member", "Member")
        authorize_task_owner(actor, users[1])
        with pytest.raises(AuthError):
            authorize_task_owner(actor, users[0])
        authorize_task_owner(CurrentUser(users[0], "admin", "Admin"), users[1])
        with Session(engine) as db, db.begin():
            db.get(User, users[1]).role = "Viewer"
        assert api.get("/test/member-only", headers=auth_header(member)).status_code == 403


def test_audit_and_session_transaction_rollback(auth_env, monkeypatch):
    settings, engine, users = auth_env

    def audit_failure(*args):
        raise RuntimeError("private-audit-failure")

    monkeypatch.setattr("packages.auth.service.audit", audit_failure)
    with client(settings) as api:
        result = api.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD})
        assert result.status_code == 500 and "private-audit-failure" not in result.text
        assert "dg_refresh" not in api.cookies
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AuthSession)) == 0


def test_schema_upgrade_constraints_downgrade_and_guard():
    with scratch_database() as (settings, engine):
        migrate(engine)
        connections = Connections(settings)
        assert connections.ready()
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "0001_auth"
            )
        migrate(engine, "downgrade", "base")
        assert not connections.ready()
        migrate(engine)
        with Session(engine) as db, db.begin():
            db.add(
                User(
                    id=str(uuid4()),
                    username="invalid",
                    password_hash="not-used",
                    role="Owner",
                    is_active=True,
                )
            )
            with pytest.raises(OperationalError) as failure:
                db.flush()
            assert failure.value.orig.args[0] == 3819
            db.rollback()
        bootstrap = BootstrapSettings(
            _env_file=None,
            **{
                **settings.model_dump(),
                "bootstrap_username": "operator",
                "bootstrap_password": PASSWORD,
            },
        )
        assert seed(bootstrap)
        assert not seed(bootstrap.model_copy(update={"bootstrap_password": bootstrap.signing_key}))
        with Session(engine) as db, db.begin():
            original = db.scalar(select(User))
            assert PASSWORD_HASHER.verify(original.password_hash, PASSWORD)
            db.add(User(username="operator", password_hash="unused", role="Member"))
            with pytest.raises(IntegrityError) as failure:
                db.flush()
            assert failure.value.orig.args[0] == 1062
            db.rollback()
        with Session(engine) as db, db.begin():
            db.add(
                AuthSession(
                    user_id=str(uuid4()),
                    refresh_hash="a" * 64,
                    csrf_hash="b" * 64,
                    expires_at=now() + timedelta(days=7),
                )
            )
            with pytest.raises(IntegrityError) as failure:
                db.flush()
            assert failure.value.orig.args[0] == 1452
            db.rollback()
        with pytest.raises(RuntimeError, match="must be empty"):
            migrate(engine, "downgrade", "base")
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(User)) == 1
            assert db.scalar(select(User)).password_hash.startswith("$argon2id$")
        connections.close()


def test_missing_and_incompatible_revision_refuse_startup():
    with scratch_database() as (settings, engine):
        with pytest.raises(ConfigurationError, match="SYSTEM_MIGRATION_REQUIRED"):
            with client(settings):
                pass
        migrate(engine)
        with engine.begin() as db:
            db.execute(text("UPDATE alembic_version SET version_num='unknown_revision'"))
        with pytest.raises(ConfigurationError, match="incompatible schema"):
            with client(settings):
                pass


def test_schema_matches_metadata_and_preserves_unrelated_tables():
    with scratch_database() as (settings, engine):
        migrate(engine)
        with engine.begin() as db:
            db.execute(text("CREATE TABLE unrelated_probe (id INT PRIMARY KEY)"))
        # Drift checking considers mapped application tables only.
        migrate(engine, "check", None)
        migrate(engine, "downgrade", "base")
        with engine.connect() as db:
            assert db.execute(text("SELECT COUNT(*) FROM unrelated_probe")).scalar() == 0


@pytest.mark.parametrize("bucket", ["user", "ip"])
def test_each_rate_bucket_is_independently_enforced(auth_env, bucket):
    settings, engine, users = auth_env
    limited = settings.model_copy(update={"login_rate_limit": 5})
    for index in range(6):
        name = "absent" if bucket == "user" else "absent" + str(index)
        host = "198.51.100." + str(index + 1) if bucket == "user" else "198.51.100.1"
        with TestClient(
            create_app(limited), base_url="http://127.0.0.1", client=(host, 50000)
        ) as api:
            result = api.post("/api/v1/auth/login", json={"username": name, "password": "wrong"})
            assert result.status_code == (401 if index < 5 else 429)


def test_refresh_logout_audit_failure_rolls_back(auth_env, monkeypatch):
    settings, engine, users = auth_env
    with client(settings) as api:
        result = login(api)
        old_refresh = api.cookies["dg_refresh"]
        csrf = result.json()["csrf_token"]

        def failure(*args):
            raise RuntimeError("private-audit-failure")

        with monkeypatch.context() as patch:
            patch.setattr("packages.auth.service.audit", failure)
            for path in ("refresh", "logout"):
                response = api.post("/api/v1/auth/" + path, headers={"X-CSRF-Token": csrf})
                assert response.status_code == 500 and "private-audit-failure" not in response.text
                assert "Set-Cookie" not in response.headers
                assert api.cookies["dg_refresh"] == old_refresh
                with Session(engine) as db:
                    row = db.scalar(select(AuthSession))
                    assert row.generation == 0 and row.revoked_at is None
        assert api.get("/api/v1/users/me", headers=auth_header(result)).status_code == 200
        assert api.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": csrf}).status_code == 200


def test_refresh_cannot_extend_absolute_lifetime(auth_env):
    settings, engine, users = auth_env
    with client(settings) as api:
        result = login(api)
        expiry = now() + timedelta(seconds=60)
        with Session(engine) as db, db.begin():
            db.scalar(select(AuthSession)).expires_at = expiry
        result = api.post(
            "/api/v1/auth/refresh", headers={"X-CSRF-Token": result.json()["csrf_token"]}
        )
        assert result.status_code == 200 and 1 <= result.json()["expires_in"] <= 60
        from http.cookies import SimpleCookie

        cookie = SimpleCookie()
        cookie.load(result.headers["Set-Cookie"])
        assert 1 <= int(cookie["dg_refresh"]["max-age"]) <= 60
        with Session(engine) as db:
            assert db.scalar(select(AuthSession)).expires_at == expiry


def test_secure_cookie_on_https_and_http_rejected(auth_env):
    settings, engine, users = auth_env
    production = settings.model_copy(update={"environment": "production"})
    with TestClient(create_app(production), base_url="http://127.0.0.1") as api:
        response = api.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD})
        assert response.status_code == 400
    with TestClient(create_app(production), base_url="https://127.0.0.1") as api:
        response = login(api)
        assert "Secure" in response.headers["Set-Cookie"]
