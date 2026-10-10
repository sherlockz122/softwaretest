"""认证接口 A01～A04（docs/04 第 10.1 节）。

A01 登录（201 + access/csrf + HttpOnly refresh cookie）
A02 刷新（200，refresh/CSRF 轮转，旧值失效）
A03 退出（204，会话撤销）
A04 当前用户（受保护）
"""

from datetime import timedelta
from functools import lru_cache

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.errors import error
from apps.api.security import (
    AuthContext,
    AuthRejection,
    get_db_session,
    require_auth,
    require_csrf,
    require_origin,
)
from packages.domain.models import AuthSession, OperationLog, User
from packages.platform.config import Settings
from packages.security.passwords import hash_password, verify_password
from packages.security.ratelimit import check_login_attempt
from packages.security.tokens import (
    as_utc,
    create_access_token,
    new_opaque_token,
    now_utc,
    sha256_hex,
)

router = APIRouter(prefix="/api/v1", tags=["auth"])

REFRESH_COOKIE = "dg_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    # 未知用户也执行一次 Argon2 校验，避免时间侧信道区分用户是否存在。
    return hash_password("defectguard-dummy-timing-value")


def _refresh_cookie_settings(settings: Settings, max_age: int) -> dict:
    return {
        "key": REFRESH_COOKIE,
        "httponly": True,
        "samesite": "strict",
        "path": REFRESH_COOKIE_PATH,
        "secure": settings.secure_cookies,
        "max_age": max_age,
    }


def _write_audit(
    session: Session, *, actor_id: str, action: str, result: str, request_id: str | None
) -> None:
    session.add(
        OperationLog(
            actor_id=actor_id,
            action=action,
            object_type="auth_session",
            result=result,
            request_id=request_id,
        )
    )


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/login", status_code=201)
def login(body: LoginBody, request: Request, session: Session = Depends(get_db_session)):
    settings: Settings = request.app.state.settings
    username = body.username.strip().lower()
    content_type = request.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        return error(request, 422, "SYSTEM_INVALID_INPUT", "请求参数无效")
    require_origin(settings, request)
    request_id = getattr(request.state, "request_id", None)

    user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
    password_ok = verify_password(
        user.password_hash if user is not None else _dummy_hash(), body.password
    )
    client_ip = request.client.host if request.client else "unknown"
    if user is None or not password_ok or not user.is_active:
        if not check_login_attempt(
            request.app.state.connections.redis,
            client_ip,
            username,
            settings.login_rate_limit_per_minute,
        ):
            return error(request, 429, "AUTH_RATE_LIMITED", "尝试过于频繁，请稍后再试")
        if user is not None:
            _write_audit(session, actor_id=user.id, action="auth.login", result="failed",
                         request_id=request_id)
            session.commit()
        return error(request, 401, "AUTH_INVALID_CREDENTIALS", "用户名或密码错误")

    refresh_token = new_opaque_token()
    csrf_token = new_opaque_token()
    session_row = AuthSession(
        user_id=user.id,
        refresh_hash=sha256_hex(refresh_token),
        csrf_hash=sha256_hex(csrf_token),
        expires_at=now_utc() + timedelta(days=settings.session_ttl_days),
    )
    session.add(session_row)
    _write_audit(session, actor_id=user.id, action="auth.login", result="succeeded",
                 request_id=request_id)
    session.commit()
    access_token = create_access_token(settings, user_id=user.id, session_id=session_row.id)
    response = JSONResponse(
        status_code=201,
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.access_token_ttl_seconds,
            "csrf_token": csrf_token,
            "user": {"id": user.id, "username": user.username, "role": user.role},
        },
        headers={"X-Request-ID": getattr(request.state, "request_id", "")},
    )
    response.set_cookie(
        value=refresh_token,
        **_refresh_cookie_settings(settings, max_age=settings.session_ttl_days * 86400),
    )
    return response


@router.post("/auth/refresh")
def refresh(request: Request, session: Session = Depends(get_db_session)):
    settings: Settings = request.app.state.settings
    refresh_token = request.cookies.get(REFRESH_COOKIE, "")
    if not refresh_token:
        raise AuthRejection(401, "AUTH_SESSION_INVALID", "会话无效或已过期")
    require_origin(settings, request)
    # 行锁 + 轮转：同一旧 refresh 的并发刷新只有一个成功，另一个拿不到旧行。
    row = session.execute(
        select(AuthSession)
        .where(AuthSession.refresh_hash == sha256_hex(refresh_token))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise AuthRejection(401, "AUTH_SESSION_INVALID", "会话无效或已过期")
    require_csrf(row, request)
    if row.revoked_at is not None or as_utc(row.expires_at) <= now_utc():
        raise AuthRejection(401, "AUTH_SESSION_INVALID", "会话无效或已过期")
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise AuthRejection(401, "AUTH_SESSION_INVALID", "会话无效或已过期")

    new_refresh = new_opaque_token()
    new_csrf = new_opaque_token()
    row.refresh_hash = sha256_hex(new_refresh)
    row.csrf_hash = sha256_hex(new_csrf)
    row.generation += 1
    _write_audit(
        session, actor_id=user.id, action="auth.refresh", result="succeeded",
        request_id=getattr(request.state, "request_id", None),
    )
    session.commit()
    access_token = create_access_token(settings, user_id=user.id, session_id=row.id)
    response = JSONResponse(
        status_code=200,
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.access_token_ttl_seconds,
            "csrf_token": new_csrf,
            "user": {"id": user.id, "username": user.username, "role": user.role},
        },
        headers={"X-Request-ID": getattr(request.state, "request_id", "")},
    )
    response.set_cookie(
        value=new_refresh,
        **_refresh_cookie_settings(settings, max_age=settings.session_ttl_days * 86400),
    )
    return response


@router.post("/auth/logout", status_code=204)
def logout(request: Request, session: Session = Depends(get_db_session)):
    settings: Settings = request.app.state.settings
    refresh_token = request.cookies.get(REFRESH_COOKIE, "")
    if not refresh_token:
        raise AuthRejection(401, "AUTH_REQUIRED", "需要登录")
    require_origin(settings, request)
    row = session.execute(
        select(AuthSession).where(AuthSession.refresh_hash == sha256_hex(refresh_token))
    ).scalar_one_or_none()
    if row is None:
        raise AuthRejection(401, "AUTH_REQUIRED", "需要登录")
    require_csrf(row, request)
    if row.revoked_at is None:
        row.revoked_at = now_utc()
        _write_audit(
            session, actor_id=row.user_id, action="auth.logout", result="succeeded",
            request_id=getattr(request.state, "request_id", None),
        )
        session.commit()
    # 204 不能有 body：JSONResponse 会把 None 渲染成 "null"，真实 HTTP 栈会拒绝。
    response = Response(
        status_code=204,
        headers={"X-Request-ID": getattr(request.state, "request_id", "")},
    )
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
    )
    return response


@router.get("/users/me")
def me(request: Request, auth: AuthContext = Depends(require_auth)):
    return {
        "id": auth.user.id,
        "username": auth.user.username,
        "role": auth.user.role,
    }
