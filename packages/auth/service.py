import hmac
import secrets
from datetime import UTC, datetime, timedelta

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.auth.security import (
    DUMMY_HASH,
    AuthError,
    CurrentUser,
    access_token,
    decode_access,
    digest,
    password_valid,
)
from packages.persistence.models import AuthSession, OperationLog, User

RATE_SCRIPT = """
local blocked = 0
for _,key in ipairs(KEYS) do
  local count = redis.call('INCR',key)
  if count == 1 then redis.call('EXPIRE',key,60) end
  if count > tonumber(ARGV[1]) then blocked = 1 end
end
return blocked
"""


def now():
    return datetime.now(UTC).replace(tzinfo=None)


def audit(db, user_id, action, session_id, request_id):
    db.add(
        OperationLog(
            actor_id=user_id,
            action=action,
            object_type="auth_session",
            object_id=session_id,
            result="succeeded",
            request_id=request_id,
            detail_json={},
        )
    )


class AuthService:
    def __init__(self, settings, connections):
        self.settings = settings
        self.engine = connections.engine
        self.redis = connections.redis

    def rate_limit(self, name: str, ip: str):
        def key(kind, value):
            hashed = hmac.digest(
                self.settings.signing_key.get_secret_value().encode(), value.encode(), "sha256"
            ).hex()
            return f"{self.settings.login_rate_prefix}:{kind}:{hashed}"

        try:
            blocked = self.redis.eval(
                RATE_SCRIPT, 2, key("user", name), key("ip", ip), self.settings.login_rate_limit
            )
        except RedisError:
            raise AuthError(503, "SYSTEM_DEPENDENCY_UNAVAILABLE") from None
        if blocked:
            raise AuthError(429, "AUTH_RATE_LIMITED")

    def response(self, user, session, refresh, csrf):
        remaining = max(1, int((session.expires_at - now()).total_seconds()))
        expires_in = min(self.settings.access_seconds, remaining)
        return (
            {
                "access_token": access_token(self.settings, user.id, session.id, expires_in),
                "token_type": "bearer",
                "expires_in": expires_in,
                "csrf_token": csrf,
                "user": {"id": user.id, "username": user.username, "role": user.role},
            },
            refresh,
            remaining,
        )

    def login(self, name, password, ip, request_id):
        self.rate_limit(name, ip)
        with Session(self.engine, expire_on_commit=False) as db, db.begin():
            user = db.scalar(select(User).where(User.username == name).with_for_update())
            valid = password_valid(user.password_hash if user else DUMMY_HASH, password)
            if not user or not valid or not user.is_active:
                raise AuthError(401, "AUTH_INVALID_CREDENTIALS")
            refresh, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session = AuthSession(
                user_id=user.id,
                refresh_hash=digest(refresh),
                csrf_hash=digest(csrf),
                expires_at=now() + timedelta(seconds=self.settings.session_seconds),
            )
            db.add(session)
            db.flush()
            audit(db, user.id, "auth.login", session.id, request_id)
            response = self.response(user, session, refresh, csrf)
        return response

    def session_operation(self, refresh, csrf, request_id, logout=False):
        if not refresh or len(refresh) > 256:
            raise AuthError(401, "AUTH_SESSION_INVALID")
        with Session(self.engine, expire_on_commit=False) as db, db.begin():
            session = db.scalar(
                select(AuthSession)
                .where(AuthSession.refresh_hash == digest(refresh))
                .with_for_update()
            )
            if not session:
                raise AuthError(401, "AUTH_SESSION_INVALID")
            if (
                not csrf
                or len(csrf) > 256
                or not hmac.compare_digest(session.csrf_hash, digest(csrf))
            ):
                raise AuthError(403, "AUTH_CSRF_REJECTED")
            user = db.get(User, session.user_id)
            if logout:
                if session.revoked_at is None:
                    session.revoked_at = now()
                    audit(db, user.id, "auth.logout", session.id, request_id)
                return None
            if session.revoked_at or session.expires_at <= now() or not user.is_active:
                raise AuthError(401, "AUTH_SESSION_INVALID")
            new_refresh, new_csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session.refresh_hash, session.csrf_hash = digest(new_refresh), digest(new_csrf)
            session.generation += 1
            audit(db, user.id, "auth.refresh", session.id, request_id)
            response = self.response(user, session, new_refresh, new_csrf)
        return response

    def current_user(self, token):
        payload = decode_access(self.settings, token)
        with Session(self.engine) as db:
            row = db.execute(
                select(User, AuthSession)
                .join(AuthSession, AuthSession.user_id == User.id)
                .where(User.id == payload["sub"], AuthSession.id == payload["sid"])
            ).first()
            if not row or not row[0].is_active or row[1].revoked_at or row[1].expires_at <= now():
                raise AuthError(401, "AUTH_REQUIRED")
            return CurrentUser(row[0].id, row[0].username, row[0].role)
