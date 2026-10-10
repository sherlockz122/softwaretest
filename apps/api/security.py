"""认证依赖：access 校验、会话活性检查、角色检查与 Origin/CSRF 校验。"""

from collections.abc import Iterator
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.models import AuthSession, User
from packages.platform.config import Settings
from packages.security.tokens import as_utc, decode_access_token, hash_matches, now_utc


class AuthRejection(Exception):
    """由统一异常处理器转为错误响应的认证拒绝。"""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


@dataclass
class AuthContext:
    user: User
    session: AuthSession


def get_db_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def origin_allowed(settings: Settings, request: Request) -> bool:
    # 浏览器请求必须与显式前端允许列表一致；非浏览器（无 Origin）不受 Origin 约束。
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return origin == settings.frontend_origin


def require_origin(settings: Settings, request: Request) -> None:
    if not origin_allowed(settings, request):
        raise AuthRejection(403, "AUTH_CSRF_REJECTED", "请求来源校验失败")


def require_csrf(session_row: AuthSession, request: Request) -> None:
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not csrf_token or not hash_matches(session_row.csrf_hash, csrf_token):
        raise AuthRejection(403, "AUTH_CSRF_REJECTED", "请求来源校验失败")


def require_auth(
    request: Request,
    session: Session = Depends(get_db_session),
) -> AuthContext:
    settings: Settings = request.app.state.settings
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthRejection(401, "AUTH_REQUIRED", "需要登录")
    try:
        payload = decode_access_token(settings, token)
    except jwt.InvalidTokenError:
        raise AuthRejection(401, "AUTH_REQUIRED", "需要登录")
    row = session.execute(
        select(AuthSession).where(AuthSession.id == payload["sid"])
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or as_utc(row.expires_at) <= now_utc():
        raise AuthRejection(401, "AUTH_REQUIRED", "需要登录")
    if row.user_id != payload["sub"]:
        raise AuthRejection(401, "AUTH_REQUIRED", "需要登录")
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise AuthRejection(401, "AUTH_REQUIRED", "需要登录")
    return AuthContext(user=user, session=row)


def require_role(*roles: str):
    def dependency(auth: AuthContext = Depends(require_auth)) -> AuthContext:
        if auth.user.role not in roles:
            raise AuthRejection(403, "AUTH_FORBIDDEN", "权限不足")
        return auth

    return dependency
