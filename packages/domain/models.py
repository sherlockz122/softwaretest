"""首批业务表模型（docs/04 第 10.4 节）。仅面向 MySQL 8 验证，不用 SQLite 代替约束验收。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import CHAR, DECIMAL, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def ascii_char(length: int) -> CHAR:
    return CHAR(length, charset="ascii", collation="ascii_bin")


def ascii_varchar(length: int) -> VARCHAR:
    return VARCHAR(length, charset="ascii", collation="ascii_bin")


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user"

    id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(ascii_varchar(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_hash: Mapped[str | None] = mapped_column(ascii_char(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(6), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(6), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        CheckConstraint("role IN ('Viewer','Member','Admin')", name="ck_user_role"),
    )


class AuthSession(Base):
    __tablename__ = "auth_session"

    id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        ascii_char(36), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    refresh_hash: Mapped[str] = mapped_column(ascii_char(64), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(ascii_char(64), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(6), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(6), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(6), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_auth_session_user_revoked", "user_id", "revoked_at"),
        Index("ix_auth_session_expires", "expires_at"),
    )


class AsyncTask(Base):
    __tablename__ = "async_task"

    id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True, default=new_uuid)
    actor_id: Mapped[str] = mapped_column(
        ascii_char(36), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(ascii_varchar(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(ascii_varchar(192), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(ascii_varchar(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(ascii_char(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")
    progress: Mapped[float] = mapped_column(DECIMAL(5, 2), nullable=False, default=0)
    processed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(6), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(6), nullable=True)
    execution_token: Mapped[str | None] = mapped_column(ascii_char(36), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(6), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(6), nullable=True)
    retry_of: Mapped[str | None] = mapped_column(
        ascii_char(36), ForeignKey("async_task.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    root_task_id: Mapped[str] = mapped_column(ascii_char(36), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_deadline: Mapped[datetime] = mapped_column(DateTime(6), nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    request_id: Mapped[str | None] = mapped_column(ascii_char(36), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(6), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(6), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','cancel_requested','succeeded','failed','cancelled')",
            name="ck_async_task_status",
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_async_task_progress"),
        CheckConstraint("processed >= 0", name="ck_async_task_processed"),
        CheckConstraint("total IS NULL OR total >= 0", name="ck_async_task_total"),
        CheckConstraint("retry_count >= 0", name="ck_async_task_retry_count"),
        UniqueConstraint("type", "scope_key", "idempotency_key", name="uq_task_idempotency"),
        Index("ix_async_task_status_deadline", "status", "queued_deadline"),
        Index("ix_async_task_status_lease", "status", "lease_until"),
        Index("ix_async_task_created_id", "created_at", "id"),
    )


class TaskOutbox(Base):
    __tablename__ = "task_outbox"

    id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        ascii_char(36), ForeignKey("async_task.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(ascii_varchar(32), nullable=False, default="execute")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(6), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(6), nullable=True)
    delivery_token: Mapped[str | None] = mapped_column(ascii_char(36), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(6), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(6), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(6), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','dispatching','sent','dead')", name="ck_task_outbox_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_task_outbox_attempts"),
        UniqueConstraint("task_id", "event_type", name="uq_outbox_task_event"),
        Index("ix_task_outbox_status_next", "status", "next_attempt_at"),
        Index("ix_task_outbox_status_lease", "status", "lease_until"),
    )


class OperationLog(Base):
    __tablename__ = "operation_log"

    id: Mapped[str] = mapped_column(ascii_char(36), primary_key=True, default=new_uuid)
    actor_id: Mapped[str] = mapped_column(
        ascii_char(36), ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(ascii_varchar(64), nullable=False)
    object_type: Mapped[str] = mapped_column(ascii_varchar(64), nullable=False)
    object_id: Mapped[str | None] = mapped_column(ascii_char(36), nullable=True)
    result: Mapped[str] = mapped_column(ascii_varchar(32), nullable=False)
    request_id: Mapped[str | None] = mapped_column(ascii_char(36), nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(6), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_operation_log_actor_created", "actor_id", "created_at"),
        Index("ix_operation_log_object", "object_type", "object_id"),
    )
