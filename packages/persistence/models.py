from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.mysql import (
    BIGINT,
    BOOLEAN,
    CHAR,
    DATETIME,
    DECIMAL,
    INTEGER,
    JSON,
    MEDIUMTEXT,
    VARCHAR,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_HEAD = "0004_commit_parsing"


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


class AsyncTask(Stamp, Base):
    __tablename__ = "async_task"
    __table_args__ = (
        UniqueConstraint("type", "scope_key", "idempotency_key", name="uq_task_request"),
        CheckConstraint(
            "status IN ('queued','running','cancel_requested','succeeded','failed','cancelled')",
            name="ck_task_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_task_progress"),
        CheckConstraint("processed >= 0 AND (total IS NULL OR total >= 0)", name="ck_task_counts"),
        CheckConstraint("retry_count >= 0 AND version >= 0", name="ck_task_versions"),
        CheckConstraint("JSON_STORAGE_SIZE(payload) <= 16384", name="ck_task_payload_size"),
        CheckConstraint(
            "result_json IS NULL OR JSON_STORAGE_SIZE(result_json) <= 4096",
            name="ck_task_result_size",
        ),
        Index("ix_task_queue_deadline", "status", "queued_deadline"),
        Index("ix_task_lease", "status", "lease_until"),
        Index("ix_task_created_id", "created_at", "id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    actor_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey("user.id", ondelete="RESTRICT"))
    type: Mapped[str] = mapped_column(VARCHAR(64, charset="ascii", collation="ascii_bin"))
    scope_key: Mapped[str] = mapped_column(VARCHAR(192, charset="ascii", collation="ascii_bin"))
    idempotency_key: Mapped[str] = mapped_column(
        VARCHAR(128, charset="ascii", collation="ascii_bin")
    )
    payload_hash: Mapped[str] = mapped_column(hash_column())
    payload: Mapped[dict] = mapped_column(JSON())
    status: Mapped[str] = mapped_column(VARCHAR(24), default="queued")
    stage: Mapped[str] = mapped_column(VARCHAR(64), default="queued")
    progress: Mapped[float] = mapped_column(DECIMAL(5, 2), default=0, server_default="0")
    processed: Mapped[int] = mapped_column(BIGINT(), default=0, server_default="0")
    total: Mapped[int | None] = mapped_column(BIGINT())
    heartbeat_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    lease_until: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    execution_token: Mapped[str | None] = mapped_column(uuid_column())
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    retry_of: Mapped[str | None] = mapped_column(
        uuid_column(), ForeignKey("async_task.id", ondelete="RESTRICT"), unique=True
    )
    root_task_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("async_task.id", ondelete="RESTRICT")
    )
    retry_count: Mapped[int] = mapped_column(INTEGER(), default=0, server_default="0")
    queued_deadline: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    result_json: Mapped[dict | None] = mapped_column(JSON(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(VARCHAR(64))
    error_message: Mapped[str | None] = mapped_column(VARCHAR(512))
    request_id: Mapped[str] = mapped_column(uuid_column())
    version: Mapped[int] = mapped_column(BIGINT(), default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=func.utc_timestamp(6), onupdate=func.utc_timestamp(6)
    )


class TaskOutbox(Stamp, Base):
    __tablename__ = "task_outbox"
    __table_args__ = (
        UniqueConstraint("task_id", "event_type", name="uq_outbox_event"),
        CheckConstraint(
            "status IN ('pending','dispatching','sent','dead')", name="ck_outbox_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
        Index("ix_outbox_lease", "status", "lease_until"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("async_task.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(
        VARCHAR(32), default="execute", server_default="execute"
    )
    status: Mapped[str] = mapped_column(VARCHAR(16), default="pending")
    attempts: Mapped[int] = mapped_column(INTEGER(), default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    lease_until: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    delivery_token: Mapped[str | None] = mapped_column(uuid_column())
    sent_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    last_error_code: Mapped[str | None] = mapped_column(VARCHAR(64))


class Repository(Stamp, Base):
    __tablename__ = "repository"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_repository_url"),
        CheckConstraint(
            "status IN ('queued','cloning','cloned','failed','cancelled')",
            name="ck_repository_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_repository_size"),
        CheckConstraint(
            "parse_status IN ('pending','queued','parsing','parsed','failed','cancelled')",
            name="ck_repository_parse_status",
        ),
        Index("ix_repository_created_id", "created_at", "id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    owner_id: Mapped[str] = mapped_column(uuid_column(), ForeignKey("user.id", ondelete="RESTRICT"))
    canonical_url: Mapped[str] = mapped_column(
        VARCHAR(1024, charset="ascii", collation="ascii_bin")
    )
    status: Mapped[str] = mapped_column(VARCHAR(24), default="queued")
    default_branch: Mapped[str | None] = mapped_column(VARCHAR(255))
    head_sha: Mapped[str | None] = mapped_column(hash_column())
    size_bytes: Mapped[int] = mapped_column(BIGINT(), default=0, server_default="0")
    storage_key: Mapped[str | None] = mapped_column(VARCHAR(192))
    latest_task_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("async_task.id", ondelete="RESTRICT")
    )
    parse_status: Mapped[str] = mapped_column(
        VARCHAR(24), default="pending", server_default="pending"
    )
    parse_root_task_id: Mapped[str | None] = mapped_column(
        uuid_column(), ForeignKey("async_task.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=func.utc_timestamp(6), onupdate=func.utc_timestamp(6)
    )


class AuthorIdentity(Stamp, Base):
    __tablename__ = "author_identity"
    __table_args__ = (
        UniqueConstraint("identity_key", "identity_version", name="uq_author_identity"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    identity_key: Mapped[str] = mapped_column(hash_column())
    name_alias: Mapped[str] = mapped_column(VARCHAR(256))
    email_hash: Mapped[str | None] = mapped_column(hash_column())
    identity_version: Mapped[str] = mapped_column(VARCHAR(64))


class GitCommit(Stamp, Base):
    __tablename__ = "git_commit"
    __table_args__ = (
        UniqueConstraint("repository_id", "sha", name="uq_commit_repository_sha"),
        CheckConstraint("parent_count >= 0", name="ck_commit_parents"),
        Index("ix_commit_event", "repository_id", "committer_time", "sha"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    repository_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("repository.id", ondelete="RESTRICT")
    )
    sha: Mapped[str] = mapped_column(hash_column())
    author_identity_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("author_identity.id", ondelete="RESTRICT")
    )
    author_time: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    committer_time: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    author_offset: Mapped[int] = mapped_column(INTEGER())
    committer_offset: Mapped[int] = mapped_column(INTEGER())
    message: Mapped[str] = mapped_column(MEDIUMTEXT())
    parents: Mapped[list] = mapped_column(JSON())
    parent_count: Mapped[int] = mapped_column(INTEGER())
    parse_status: Mapped[str] = mapped_column(VARCHAR(32))
    parser_version: Mapped[str] = mapped_column(VARCHAR(64))


class FileChange(Stamp, Base):
    __tablename__ = "file_change"
    __table_args__ = (
        UniqueConstraint("commit_id", "ordinal", name="uq_file_ordinal"),
        CheckConstraint(
            "ordinal >= 0 AND (insertions IS NULL OR insertions >= 0) "
            "AND (deletions IS NULL OR deletions >= 0) AND (old_loc IS NULL OR old_loc >= 0)",
            name="ck_file_counts",
        ),
        Index("ix_file_commit", "commit_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    id: Mapped[str] = mapped_column(uuid_column(), primary_key=True)
    commit_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("git_commit.id", ondelete="RESTRICT")
    )
    ordinal: Mapped[int] = mapped_column(INTEGER())
    old_path: Mapped[str | None] = mapped_column(MEDIUMTEXT())
    new_path: Mapped[str | None] = mapped_column(MEDIUMTEXT())
    path_bytes_hash: Mapped[str] = mapped_column(hash_column())
    old_blob: Mapped[str] = mapped_column(hash_column())
    new_blob: Mapped[str] = mapped_column(hash_column())
    change_type: Mapped[str] = mapped_column(VARCHAR(16))
    insertions: Mapped[int | None] = mapped_column(BIGINT())
    deletions: Mapped[int | None] = mapped_column(BIGINT())
    old_loc: Mapped[int | None] = mapped_column(BIGINT())
    is_binary: Mapped[bool] = mapped_column(BOOLEAN())
    content_status: Mapped[str] = mapped_column(VARCHAR(32))
    diff_text: Mapped[str | None] = mapped_column(MEDIUMTEXT())
    line_numbers: Mapped[dict | None] = mapped_column(JSON())
    parser_version: Mapped[str] = mapped_column(VARCHAR(64))


class ParseCheckpoint(Stamp, Base):
    __tablename__ = "parse_checkpoint"
    __table_args__ = (
        UniqueConstraint("repository_id", name="uq_checkpoint_repository"),
        CheckConstraint(
            "processed >= 0 AND (total IS NULL OR total >= processed) "
            "AND (commit_limit IS NULL OR commit_limit > 0)",
            name="ck_checkpoint_counts",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    root_task_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("async_task.id", ondelete="RESTRICT"), primary_key=True
    )
    repository_id: Mapped[str] = mapped_column(
        uuid_column(), ForeignKey("repository.id", ondelete="RESTRICT")
    )
    head_sha: Mapped[str | None] = mapped_column(hash_column())
    commit_limit: Mapped[int | None] = mapped_column(BIGINT())
    parser_version: Mapped[str] = mapped_column(VARCHAR(64))
    plan_hash: Mapped[str | None] = mapped_column(hash_column())
    total: Mapped[int | None] = mapped_column(BIGINT())
    processed: Mapped[int] = mapped_column(BIGINT(), default=0, server_default="0")
    last_sha: Mapped[str | None] = mapped_column(hash_column())
