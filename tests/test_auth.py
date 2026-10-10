"""SEC-AUTH：认证端点与安全属性集成测试（docs/04 第 10.1 节）。

覆盖 A01 登录、A02 刷新轮转、A03 退出、A04 当前用户，以及
Origin/CSRF 校验、限流、并发刷新与角色边界。
需要运行中的 MySQL 与 Redis（pytest -m integration）。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from apps.api.application import create_app
from apps.api.security import AuthContext, require_role
from packages.domain.models import AuthSession, OperationLog, User
from packages.platform.config import load_settings
from packages.security.passwords import hash_password
from packages.security.tokens import decode_access_token, sha256_hex

pytestmark = pytest.mark.integration

ORIGIN = "http://auth.test.local"
REFRESH_COOKIE = "dg_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/users/me"


@pytest.fixture(scope="module")
def client():
    settings = load_settings(frontend_origin=ORIGIN)
    app = create_app(settings)

    @app.get("/api/v1/test/admin-only")
    def admin_only(auth: AuthContext = Depends(require_role("Admin"))):
        return {"ok": True}

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_rate_limit_keys(client):
    redis = client.app.state.connections.redis
    for key in redis.scan_iter(match="defectguard:login:*"):
        redis.delete(key)
    client.cookies.clear()  # 模块级 client 的 jar 不跨用例泄漏
    yield


@pytest.fixture()
def make_user(client):
    created_user_ids = []

    def factory(*, username: str, password: str, role: str = "Member", active: bool = True) -> str:
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

    # 先清审计与会话（外键引用 user），再删用户
    with client.app.state.session_factory() as session:
        for user_id in created_user_ids:
            session.execute(delete(OperationLog).where(OperationLog.actor_id == user_id))
            session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
            session.execute(delete(User).where(User.id == user_id))
        session.commit()


def _login(client, username, password, *, origin=ORIGIN):
    headers = {"Origin": origin} if origin is not None else None
    return client.post(
        LOGIN_URL, json={"username": username, "password": password}, headers=headers
    )


def _refresh_cookie(client):
    return client.cookies.get(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


def _set_refresh_cookie(client, value):
    # 手动回放 refresh：先清空 jar 避免与服务器 Set-Cookie 产生同名冲突。
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE, value, path=REFRESH_COOKIE_PATH)


def _refresh_from_response(response):
    return response.headers["set-cookie"].split(";")[0].split("=", 1)[1]


def test_login_success_contract(client, make_user):
    user_id = make_user(username="authuser1", password="authuser1-pass-123456")
    response = _login(client, "AuthUser1", "authuser1-pass-123456")  # 用户名大小写规范化
    assert response.status_code == 201
    body = response.json()
    settings = client.app.state.settings
    payload = decode_access_token(settings, body["access_token"])
    assert payload["sub"] == user_id
    assert payload["sid"]
    assert payload["iss"] == settings.jwt_issuer
    assert payload["aud"] == settings.jwt_audience
    assert payload["exp"] > payload["iat"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_ttl_seconds
    assert body["user"] == {"id": user_id, "username": "authuser1", "role": "Member"}

    refresh_token = _refresh_cookie(client)
    assert refresh_token
    set_cookie = response.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    assert "path=/api/v1/auth" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()  # 开发环境不强制 Secure

    with client.app.state.session_factory() as session:
        row = session.execute(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).scalar_one()
        assert row.refresh_hash == sha256_hex(refresh_token)
        assert row.csrf_hash == sha256_hex(body["csrf_token"])
        assert row.revoked_at is None
        assert refresh_token not in row.refresh_hash  # 仅保存哈希


def test_login_rejects_bad_credentials_uniformly(client, make_user):
    user_id = make_user(username="authuser2", password="authuser2-pass-123456")
    wrong = _login(client, "authuser2", "wrong-password-999")
    unknown = _login(client, "no-such-user", "whatever-password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["code"] == unknown.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert wrong.json()["message"] == unknown.json()["message"]  # 不区分用户是否存在
    assert set(wrong.json()) == {"code", "message", "request_id"}
    assert "set-cookie" not in wrong.headers

    with client.app.state.session_factory() as session:
        assert session.execute(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).scalar_one_or_none() is None
        logs = session.execute(
            select(OperationLog).where(OperationLog.actor_id == user_id)
        ).scalars().all()
        assert len(logs) == 1  # 仅已知用户的失败写审计，未知用户不落库
        assert logs[0].action == "auth.login"
        assert logs[0].result == "failed"


def test_login_disabled_user_rejected(client, make_user):
    make_user(username="authuser3", password="authuser3-pass-123456", active=False)
    response = _login(client, "authuser3", "authuser3-pass-123456")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"


def test_login_rate_limited(client, make_user):
    make_user(username="ratelimit-user", password="ratelimit-user-pass-123456")
    settings = load_settings(frontend_origin=ORIGIN, login_rate_limit_per_minute=3)
    with TestClient(create_app(settings)) as limited:
        for _ in range(3):
            response = _login(limited, "ratelimit-user", "wrong-password")
            assert response.status_code == 401
        response = _login(limited, "ratelimit-user", "wrong-password")
        assert response.status_code == 429
        assert response.json()["code"] == "AUTH_RATE_LIMITED"
        # 同一来源 IP 的其它用户名同样受限（IP 维度固定窗口）
        response = _login(limited, "ratelimit-other", "wrong-password")
        assert response.status_code == 429
        # 限流只累计失败尝试：正确密码仍可登录，避免锁定合法账号
        response = _login(limited, "ratelimit-user", "ratelimit-user-pass-123456")
        assert response.status_code == 201


def test_refresh_rotates_and_revokes_old(client, make_user):
    user_id = make_user(username="authuser4", password="authuser4-pass-123456")
    body = _login(client, "authuser4", "authuser4-pass-123456").json()
    old_refresh = _refresh_cookie(client)
    old_csrf = body["csrf_token"]

    def do_refresh(csrf):
        return client.post(REFRESH_URL, headers={"X-CSRF-Token": csrf, "Origin": ORIGIN})

    response = do_refresh(old_csrf)
    assert response.status_code == 200
    new_body = response.json()
    new_refresh = _refresh_cookie(client)
    assert new_refresh and new_refresh != old_refresh
    assert new_body["csrf_token"] != old_csrf
    decode_access_token(client.app.state.settings, new_body["access_token"])

    # 旧 refresh 复用 → 401
    _set_refresh_cookie(client, old_refresh)
    response = do_refresh(old_csrf)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_SESSION_INVALID"

    # 新 refresh 配旧 csrf → 403
    _set_refresh_cookie(client, new_refresh)
    response = do_refresh(old_csrf)
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_CSRF_REJECTED"

    # 新对旧对 → 200，generation 同步推进；轮转值取自响应头而非 jar
    response = do_refresh(new_body["csrf_token"])
    assert response.status_code == 200

    with client.app.state.session_factory() as session:
        row = session.execute(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).scalar_one()
        assert row.generation == 2
        assert row.refresh_hash == sha256_hex(_refresh_from_response(response))


def test_refresh_requires_csrf_and_origin(client, make_user):
    user_id = make_user(username="authuser5", password="authuser5-pass-123456")
    body = _login(client, "authuser5", "authuser5-pass-123456").json()
    csrf = body["csrf_token"]
    refresh = _refresh_cookie(client)
    client.cookies.clear()

    def refresh_with(csrf_token, origin=ORIGIN):
        headers = {"X-CSRF-Token": csrf_token}
        if origin is not None:
            headers["Origin"] = origin
        return client.post(REFRESH_URL, headers=headers)

    assert refresh_with(csrf).status_code == 401  # 无 cookie
    _set_refresh_cookie(client, refresh)
    assert refresh_with("").status_code == 403  # 缺 CSRF
    assert refresh_with("wrong-csrf-value").status_code == 403
    assert refresh_with(csrf, origin="http://evil.example").status_code == 403
    for response in (refresh_with(""), refresh_with("wrong-csrf-value")):
        assert response.json()["code"] == "AUTH_CSRF_REJECTED"
    # 前述失败均未轮转；无 Origin 的非浏览器客户端放行
    assert refresh_with(csrf, origin=None).status_code == 200
    with client.app.state.session_factory() as session:
        row = session.execute(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).scalar_one()
        assert row.generation == 1


def test_refresh_concurrent_single_winner(client, make_user):
    user_id = make_user(username="authuser6", password="authuser6-pass-123456")
    body = _login(client, "authuser6", "authuser6-pass-123456").json()
    refresh = _refresh_cookie(client)
    csrf = body["csrf_token"]

    def refresh_once():
        return client.post(
            REFRESH_URL,
            headers={"X-CSRF-Token": csrf, "Origin": ORIGIN},
            cookies={REFRESH_COOKIE: refresh},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _: refresh_once(), range(2)))
    assert statuses == [200, 401]  # 行锁轮转：同一旧 refresh 仅一个成功

    with client.app.state.session_factory() as session:
        row = session.execute(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).scalar_one()
        assert row.generation == 1


def test_logout_revokes_session_and_is_idempotent(client, make_user):
    user_id = make_user(username="authuser7", password="authuser7-pass-123456")
    body = _login(client, "authuser7", "authuser7-pass-123456").json()
    access = body["access_token"]
    refresh = _refresh_cookie(client)
    csrf = body["csrf_token"]

    assert client.get(ME_URL, headers={"Authorization": f"Bearer {access}"}).status_code == 200

    client.cookies.clear()
    response = client.post(LOGOUT_URL, headers={"X-CSRF-Token": csrf, "Origin": ORIGIN})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"

    _set_refresh_cookie(client, refresh)
    response = client.post(LOGOUT_URL, headers={"X-CSRF-Token": "wrong", "Origin": ORIGIN})
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_CSRF_REJECTED"

    response = client.post(LOGOUT_URL, headers={"X-CSRF-Token": csrf, "Origin": ORIGIN})
    assert response.status_code == 204
    assert "max-age=0" in response.headers.get("set-cookie", "").lower()
    assert client.get(ME_URL, headers={"Authorization": f"Bearer {access}"}).status_code == 401

    # 已撤销会话再次退出仍 204；refresh 已失效
    _set_refresh_cookie(client, refresh)
    response = client.post(LOGOUT_URL, headers={"X-CSRF-Token": csrf, "Origin": ORIGIN})
    assert response.status_code == 204
    _set_refresh_cookie(client, refresh)
    response = client.post(REFRESH_URL, headers={"X-CSRF-Token": csrf, "Origin": ORIGIN})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_SESSION_INVALID"

    with client.app.state.session_factory() as session:
        row = session.execute(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).scalar_one()
        assert row.revoked_at is not None


def test_me_requires_valid_access_token(client, make_user):
    user_id = make_user(username="authuser8", password="authuser8-pass-123456")
    body = _login(client, "authuser8", "authuser8-pass-123456").json()

    response = client.get(ME_URL)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"
    response = client.get(ME_URL, headers={"Authorization": "Bearer garbage"})
    assert response.status_code == 401

    response = client.get(ME_URL, headers={"Authorization": f"Bearer {body['access_token']}"})
    assert response.status_code == 200
    assert response.json() == {"id": user_id, "username": "authuser8", "role": "Member"}

    # 过期 access（同密钥手工构造）→ 401
    settings = client.app.state.settings
    payload = decode_access_token(settings, body["access_token"])
    expired = jwt.encode(
        {
            "sub": payload["sub"],
            "sid": payload["sid"],
            "iat": payload["iat"] - 3600,
            "exp": payload["iat"] - 1800,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.signing_key.get_secret_value(),
        algorithm="HS256",
    )
    response = client.get(ME_URL, headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_access_rejected_after_deactivation_or_expiry(client, make_user):
    user_id = make_user(username="authuser9", password="authuser9-pass-123456")
    body = _login(client, "authuser9", "authuser9-pass-123456").json()
    access = body["access_token"]
    csrf = body["csrf_token"]
    assert client.get(ME_URL, headers={"Authorization": f"Bearer {access}"}).status_code == 200

    with client.app.state.session_factory() as session:
        session.get(User, user_id).is_active = False
        session.commit()
    assert client.get(ME_URL, headers={"Authorization": f"Bearer {access}"}).status_code == 401
    response = client.post(REFRESH_URL, headers={"X-CSRF-Token": csrf, "Origin": ORIGIN})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_SESSION_INVALID"

    # 重新启用但会话过期 → 同样拒绝
    with client.app.state.session_factory() as session:
        session.get(User, user_id).is_active = True
        row = session.execute(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).scalar_one()
        row.expires_at = row.expires_at - timedelta(days=30)
        session.commit()
    assert client.get(ME_URL, headers={"Authorization": f"Bearer {access}"}).status_code == 401
    response = client.post(REFRESH_URL, headers={"X-CSRF-Token": csrf, "Origin": ORIGIN})
    assert response.status_code == 401


def test_require_role_enforces_admin_boundary(client, make_user):
    make_user(username="authuser10", password="authuser10-pass-123456")
    member_body = _login(client, "authuser10", "authuser10-pass-123456").json()
    response = client.get(
        "/api/v1/test/admin-only",
        headers={"Authorization": f"Bearer {member_body['access_token']}"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_FORBIDDEN"

    make_user(username="authuser11", password="authuser11-pass-123456", role="Admin")
    admin_body = _login(client, "authuser11", "authuser11-pass-123456").json()
    response = client.get(
        "/api/v1/test/admin-only",
        headers={"Authorization": f"Bearer {admin_body['access_token']}"},
    )
    assert response.status_code == 200


def test_login_content_type_and_origin_validation(client, make_user):
    make_user(username="authuser12", password="authuser12-pass-123456")
    response = client.post(
        LOGIN_URL, data="username=authuser12&password=x", headers={"Origin": ORIGIN}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "SYSTEM_INVALID_INPUT"

    response = _login(client, "authuser12", "authuser12-pass-123456", origin="http://evil.example")
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_CSRF_REJECTED"

    # 无 Origin 的非浏览器客户端放行
    response = _login(client, "authuser12", "authuser12-pass-123456", origin=None)
    assert response.status_code == 201
