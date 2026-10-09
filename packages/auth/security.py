import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
DUMMY_HASH = PASSWORD_HASHER.hash("non-user-dummy-value")


class AuthError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    role: str


def username(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise ValueError("invalid username")
    return value.lower()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_valid(stored: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def access_token(settings, user_id: str, session_id: str, expires_in: int | None = None) -> str:
    now = int(datetime.now(UTC).timestamp())
    return jwt.encode(
        {
            "sub": user_id,
            "sid": session_id,
            "iat": now,
            "exp": now + (expires_in or settings.access_seconds),
            "iss": "defectguard",
            "aud": "defectguard-web",
        },
        settings.signing_key.get_secret_value(),
        algorithm="HS256",
    )


def decode_access(settings, token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.signing_key.get_secret_value(),
            algorithms=["HS256"],
            issuer="defectguard",
            audience="defectguard-web",
            options={"require": ["sub", "sid", "iat", "exp", "iss", "aud"]},
        )
        UUID(payload["sub"])
        UUID(payload["sid"])
        return payload
    except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
        raise AuthError(401, "AUTH_REQUIRED") from None


def require_role(user: CurrentUser, role: str) -> CurrentUser:
    if {"Viewer": 0, "Member": 1, "Admin": 2}.get(user.role, -1) < {
        "Viewer": 0,
        "Member": 1,
        "Admin": 2,
    }[role]:
        raise AuthError(403, "AUTH_FORBIDDEN")
    return user


def authorize_task_owner(user: CurrentUser, actor_id: str) -> None:
    """actor_id must come from the persisted task, never from the caller's body."""
    require_role(user, "Member")
    if user.role != "Admin" and user.id != actor_id:
        raise AuthError(403, "AUTH_FORBIDDEN")
