"""访问令牌（JWT）与不透明 refresh/CSRF 令牌。

JWT 由服务端固定 HS256 并强制 iss/aud/exp；refresh 随机强度至少 256 bit，
仅在数据库保存 SHA-256。
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt

from packages.platform.config import Settings

JWT_ALGORITHM = "HS256"


def now_utc() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    # MySQL DATETIME 读取为 naive；数据库时间一律按 UTC 写入。
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def create_access_token(settings: Settings, *, user_id: str, session_id: str) -> str:
    issued = now_utc()
    payload = {
        "sub": user_id,
        "sid": session_id,
        "iat": issued,
        "exp": issued + timedelta(seconds=settings.access_token_ttl_seconds),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode(payload, settings.signing_key.get_secret_value(), algorithm=JWT_ALGORITHM)


def decode_access_token(settings: Settings, token: str) -> dict:
    """校验签名与声明；无效/过期/算法不符一律抛出 jwt 异常。"""
    return jwt.decode(
        token,
        settings.signing_key.get_secret_value(),
        algorithms=[JWT_ALGORITHM],
        options={"require": ["sub", "sid", "iat", "exp", "iss", "aud"]},
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )


def new_opaque_token() -> str:
    # token_urlsafe(32) 提供 256 bit 随机强度。
    return secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_matches(stored_hash: str, value: str) -> bool:
    return hmac.compare_digest(stored_hash, sha256_hex(value))
