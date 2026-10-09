from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.mysql import BIGINT, BOOLEAN, CHAR, DATETIME, JSON, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_HEAD = "0001_auth"


class Base(DeclarativeBase):
    pass


def uuid_column():
    return CHAR(36, charset="ascii", collation="ascii_bin")


def hash_column():
    return CHAR(64, charset="ascii", collation="ascii_bin")


class Stamp:
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=func.utc_timestamp(6)
    )


class User(Stamp, Base):
    __tablename__ = "user"
    __table_args__ = (
        CheckConstraint("role IN ('Viewer','Member','Admin')", name="ck_user_role"),
        CheckConstraint("is_active IN (0,1)", name="ck_user_active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(
        VARCHAR(64, charset="ascii", collation="ascii_bin"), unique=True
    )
    password_hash: Mapped[str] = mapped_column(VARCHAR(255))
    role: Mapped[str] = mapped_column(VARCHAR(16, charset="ascii", collation="ascii_bin"))
    is_active: Mapped[bool] = mapped_column(BOOLEAN(), default=True)
    email_hash: Mapped[str | None] = mapped_column(hash_column())
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=func.utc_timestamp(6), onupdate=func.utc_timestamp(6)
    )


class AuthSession(Stamp, Base):
    __tablename__ = "auth_session"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_session_generation"),
        Index("ix_session_user_revoked", "user_id", "revoked_at"),
        Index("ix_session_expires", "expires_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey("user.id", ondelete="RESTRICT"))
    refresh_hash: Mapped[str] = mapped_column(hash_column(), unique=True)
    csrf_hash: Mapped[str] = mapped_column(hash_column())
    generation: Mapped[int] = mapped_column(BIGINT(), default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=func.utc_timestamp(6), onupdate=func.utc_timestamp(6)
    )


class OperationLog(Stamp, Base):
    __tablename__ = "operation_log"
    __table_args__ = (
        CheckConstraint("JSON_STORAGE_SIZE(detail_json) <= 4096", name="ck_audit_detail_size"),
        Index("ix_audit_actor_created", "actor_id", "created_at"),
        Index("ix_audit_object", "object_type", "object_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey("user.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(VARCHAR(64))
    object_type: Mapped[str] = mapped_column(VARCHAR(64))
    object_id: Mapped[str] = mapped_column(uuid_column())
    result: Mapped[str] = mapped_column(VARCHAR(32))
    request_id: Mapped[str] = mapped_column(uuid_column())
    detail_json: Mapped[dict] = mapped_column(JSON())
