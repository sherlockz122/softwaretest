"""Frozen repository DDL and whole-path rollback protection."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0003_repositories"
down_revision = "0002_tasks"
branch_labels = None
depends_on = None


def uuid():
    return mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def upgrade():
    op.create_table(
        "repository",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column(
            "owner_id", uuid(), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "canonical_url",
            mysql.VARCHAR(1024, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("default_branch", sa.String(255)),
        sa.Column("head_sha", mysql.CHAR(64, charset="ascii", collation="ascii_bin")),
        sa.Column("size_bytes", mysql.BIGINT(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(192)),
        sa.Column(
            "latest_task_id",
            uuid(),
            sa.ForeignKey("async_task.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.UniqueConstraint("canonical_url", name="uq_repository_url"),
        sa.CheckConstraint(
            "status IN ('queued','cloning','cloned','failed','cancelled')",
            name="ck_repository_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_repository_size"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_repository_created_id", "repository", ["created_at", "id"])


def downgrade():
    target = context.get_revision_argument()
    tables = ["repository"]
    if target != "0002_tasks":
        tables += ["async_task", "task_outbox"]
    if target is None:
        tables += ["user", "auth_session", "operation_log"]
    connection = op.get_bind()
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar() for table in tables
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    op.drop_table("repository")
