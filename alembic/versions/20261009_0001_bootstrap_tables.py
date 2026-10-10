"""bootstrap 首批五张基础表（docs/04 第 10.4 节）：
user、auth_session、async_task、task_outbox、operation_log。

Revision ID: 20261009_0001
Revises:
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import CHAR, DECIMAL, VARCHAR

from alembic import op

revision = "20261009_0001"
down_revision = None
branch_labels = None
depends_on = None


def ascii_char(length: int) -> CHAR:
    return CHAR(length, charset="ascii", collation="ascii_bin")


def ascii_varchar(length: int) -> VARCHAR:
    return VARCHAR(length, charset="ascii", collation="ascii_bin")


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", ascii_char(36), primary_key=True),
        sa.Column("username", ascii_varchar(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_hash", ascii_char(64), nullable=True),
        sa.Column("created_at", sa.DateTime(6), nullable=False),
        sa.Column("updated_at", sa.DateTime(6), nullable=False),
        sa.UniqueConstraint("username", name="uq_user_username"),
        sa.CheckConstraint("role IN ('Viewer','Member','Admin')", name="ck_user_role"),
    )

    op.create_table(
        "auth_session",
        sa.Column("id", ascii_char(36), primary_key=True),
        sa.Column("user_id", ascii_char(36), nullable=False),
        sa.Column("refresh_hash", ascii_char(64), nullable=False),
        sa.Column("csrf_hash", ascii_char(64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(6), nullable=False),
        sa.Column("revoked_at", sa.DateTime(6), nullable=True),
        sa.Column("created_at", sa.DateTime(6), nullable=False),
        sa.Column("updated_at", sa.DateTime(6), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_auth_session_user"),
        sa.UniqueConstraint("refresh_hash", name="uq_auth_session_refresh"),
    )
    op.create_index("ix_auth_session_user_revoked", "auth_session", ["user_id", "revoked_at"])
    op.create_index("ix_auth_session_expires", "auth_session", ["expires_at"])

    op.create_table(
        "async_task",
        sa.Column("id", ascii_char(36), primary_key=True),
        sa.Column("actor_id", ascii_char(36), nullable=False),
        sa.Column("type", ascii_varchar(64), nullable=False),
        sa.Column("scope_key", ascii_varchar(192), nullable=False),
        sa.Column("idempotency_key", ascii_varchar(128), nullable=False),
        sa.Column("payload_hash", ascii_char(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(64), nullable=False, server_default="queued"),
        sa.Column("progress", DECIMAL(5, 2), nullable=False, server_default="0"),
        sa.Column("processed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total", sa.BigInteger(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(6), nullable=True),
        sa.Column("lease_until", sa.DateTime(6), nullable=True),
        sa.Column("execution_token", ascii_char(36), nullable=True),
        sa.Column("started_at", sa.DateTime(6), nullable=True),
        sa.Column("finished_at", sa.DateTime(6), nullable=True),
        sa.Column("retry_of", ascii_char(36), nullable=True),
        sa.Column("root_task_id", ascii_char(36), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued_deadline", sa.DateTime(6), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("request_id", ascii_char(36), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(6), nullable=False),
        sa.Column("updated_at", sa.DateTime(6), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"], name="fk_async_task_actor"),
        sa.ForeignKeyConstraint(["retry_of"], ["async_task.id"], name="fk_async_task_retry_of"),
        sa.UniqueConstraint(
            "type", "scope_key", "idempotency_key", name="uq_task_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','cancel_requested','succeeded','failed','cancelled')",
            name="ck_async_task_status",
        ),
        sa.CheckConstraint("progress >= 0 AND progress <= 100", name="ck_async_task_progress"),
        sa.CheckConstraint("processed >= 0", name="ck_async_task_processed"),
        sa.CheckConstraint("total IS NULL OR total >= 0", name="ck_async_task_total"),
        sa.CheckConstraint("retry_count >= 0", name="ck_async_task_retry_count"),
    )
    op.create_index("ix_async_task_status_deadline", "async_task", ["status", "queued_deadline"])
    op.create_index("ix_async_task_status_lease", "async_task", ["status", "lease_until"])
    op.create_index("ix_async_task_created_id", "async_task", ["created_at", "id"])

    op.create_table(
        "task_outbox",
        sa.Column("id", ascii_char(36), primary_key=True),
        sa.Column("task_id", ascii_char(36), nullable=False),
        sa.Column("event_type", ascii_varchar(32), nullable=False, server_default="execute"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(6), nullable=False),
        sa.Column("lease_until", sa.DateTime(6), nullable=True),
        sa.Column("delivery_token", ascii_char(36), nullable=True),
        sa.Column("sent_at", sa.DateTime(6), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(6), nullable=False),
        sa.Column("updated_at", sa.DateTime(6), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["async_task.id"], name="fk_task_outbox_task"),
        sa.UniqueConstraint("task_id", "event_type", name="uq_outbox_task_event"),
        sa.CheckConstraint(
            "status IN ('pending','dispatching','sent','dead')", name="ck_task_outbox_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_task_outbox_attempts"),
    )
    op.create_index("ix_task_outbox_status_next", "task_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_task_outbox_status_lease", "task_outbox", ["status", "lease_until"])

    op.create_table(
        "operation_log",
        sa.Column("id", ascii_char(36), primary_key=True),
        sa.Column("actor_id", ascii_char(36), nullable=False),
        sa.Column("action", ascii_varchar(64), nullable=False),
        sa.Column("object_type", ascii_varchar(64), nullable=False),
        sa.Column("object_id", ascii_char(36), nullable=True),
        sa.Column("result", ascii_varchar(32), nullable=False),
        sa.Column("request_id", ascii_char(36), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(6), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["user.id"], name="fk_operation_log_actor"),
    )
    op.create_index("ix_operation_log_actor_created", "operation_log", ["actor_id", "created_at"])
    op.create_index("ix_operation_log_object", "operation_log", ["object_type", "object_id"])


def downgrade() -> None:
    op.drop_table("operation_log")
    op.drop_table("task_outbox")
    op.drop_table("async_task")
    op.drop_table("auth_session")
    op.drop_table("user")
