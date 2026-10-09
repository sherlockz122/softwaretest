"""Authentication foundation. Definitions are frozen in this revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001_auth"
down_revision = None
branch_labels = None
depends_on = None


def uuid():
    return mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def hash_type():
    return mysql.CHAR(64, charset="ascii", collation="ascii_bin")


def stamps(updated=False):
    columns = [
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("UTC_TIMESTAMP(6)"),
        )
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("UTC_TIMESTAMP(6)"),
            )
        )
    return columns


def upgrade():
    op.create_table(
        "user",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column(
            "username", mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role", mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("email_hash", hash_type(), nullable=True),
        *stamps(True),
        sa.UniqueConstraint("username", name="uq_user_username"),
        sa.CheckConstraint("role IN ('Viewer','Member','Admin')", name="ck_user_role"),
        sa.CheckConstraint("is_active IN (0,1)", name="ck_user_active"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "auth_session",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column("user_id", uuid(), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("refresh_hash", hash_type(), nullable=False),
        sa.Column("csrf_hash", hash_type(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
        *stamps(True),
        sa.UniqueConstraint("refresh_hash", name="uq_session_refresh"),
        sa.CheckConstraint("generation >= 0", name="ck_session_generation"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_session_user_revoked", "auth_session", ["user_id", "revoked_at"])
    op.create_index("ix_session_expires", "auth_session", ["expires_at"])
    op.create_table(
        "operation_log",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column(
            "actor_id", uuid(), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_type", sa.String(64), nullable=False),
        sa.Column("object_id", uuid(), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("request_id", uuid(), nullable=False),
        sa.Column("detail_json", mysql.JSON(), nullable=False),
        *stamps(),
        sa.CheckConstraint("JSON_STORAGE_SIZE(detail_json) <= 4096", name="ck_audit_detail_size"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_audit_actor_created", "operation_log", ["actor_id", "created_at"])
    op.create_index("ix_audit_object", "operation_log", ["object_type", "object_id"])


def downgrade():
    connection = op.get_bind()
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
        for table in ("user", "auth_session", "operation_log")
    ):
        raise RuntimeError("Downgrade refused: authentication tables must be empty")
    op.drop_table("operation_log")
    op.drop_table("auth_session")
    op.drop_table("user")
