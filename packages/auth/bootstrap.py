"""Explicit one-time administrator seed; never reset an existing account."""

from uuid import uuid4

from pydantic import SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.auth.security import PASSWORD_HASHER, username
from packages.persistence.models import OperationLog, User
from packages.platform.config import ConfigurationError, Settings
from packages.platform.connections import Connections


class BootstrapSettings(Settings):
    bootstrap_username: str
    bootstrap_password: SecretStr

    @field_validator("bootstrap_username")
    @classmethod
    def name(cls, value):
        return username(value)

    @field_validator("bootstrap_password")
    @classmethod
    def password(cls, value):
        if len(value.get_secret_value()) < 12 or len(value.get_secret_value()) > 1024:
            raise ValueError("explicit password must have 12 to 1024 characters")
        return value


def seed(settings: BootstrapSettings) -> bool:
    connections = Connections(settings)
    try:
        connections.require_schema()
        with Session(connections.engine) as db, db.begin():
            existing = db.scalar(
                select(User).where(User.username == settings.bootstrap_username).with_for_update()
            )
            if existing:
                if existing.role != "Admin" or not existing.is_active:
                    raise ConfigurationError(
                        "Bootstrap refused: existing account requires explicit review"
                    )
                return False
            user = User(
                id=str(uuid4()),
                username=settings.bootstrap_username,
                password_hash=PASSWORD_HASHER.hash(settings.bootstrap_password.get_secret_value()),
                role="Admin",
                is_active=True,
            )
            db.add(user)
            db.flush()
            db.add(
                OperationLog(
                    actor_id=user.id,
                    action="user.bootstrap",
                    object_type="user",
                    object_id=user.id,
                    result="succeeded",
                    request_id=str(uuid4()),
                    detail_json={"source": "explicit_bootstrap"},
                )
            )
        return True
    finally:
        connections.close()


def main():
    from pydantic import ValidationError

    try:
        settings = BootstrapSettings()
    except ValidationError:
        raise SystemExit(
            "Bootstrap configuration missing/invalid; supply explicit username and password"
        ) from None
    try:
        created = seed(settings)
    except Exception:
        raise SystemExit(
            "Bootstrap failed; check schema/configuration or review existing account"
        ) from None
    print(
        "Administrator created"
        if created
        else "Administrator already exists; credentials unchanged"
    )


if __name__ == "__main__":
    main()
