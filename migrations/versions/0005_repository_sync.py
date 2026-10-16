"""Frozen sync DDL; preserve existing parsing windows and guard the full downgrade path."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0005_repository_sync"
down_revision = "0004_commit_parsing"
branch_labels = None
depends_on = None
DDL = (
    """CREATE TABLE sync_window (
	root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	repository_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	base_head_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	base_storage_key VARCHAR(192) NOT NULL,
	base_branch VARCHAR(255),
	head_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	storage_key VARCHAR(192),
	default_branch VARCHAR(255),
	size_bytes BIGINT NOT NULL DEFAULT '0',
	relation VARCHAR(24) NOT NULL DEFAULT 'pending',
	parser_version VARCHAR(64) NOT NULL,
	plan_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	total BIGINT,
	processed BIGINT NOT NULL DEFAULT '0',
	last_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
	PRIMARY KEY (root_task_id),
	CONSTRAINT ck_sync_counts CHECK (processed >= 0 AND (total IS NULL OR total >= processed)
        AND size_bytes >= 0),
	CONSTRAINT ck_sync_relation CHECK
        (relation IN ('pending','initial','unchanged','fast_forward','requires_review')),
	FOREIGN KEY(root_task_id) REFERENCES async_task (id) ON DELETE RESTRICT,
	FOREIGN KEY(repository_id) REFERENCES repository (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE INDEX ix_sync_repository ON sync_window (repository_id, created_at, root_task_id)""",
)


def upgrade():
    op.add_column(
        "repository",
        sa.Column("sync_status", sa.String(24), nullable=False, server_default="pending"),
    )
    op.add_column(
        "repository",
        sa.Column("sync_root_task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin")),
    )
    op.create_foreign_key(
        "fk_repository_sync_root",
        "repository",
        "async_task",
        ["sync_root_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_repository_sync_status",
        "repository",
        "sync_status IN ('pending','queued','syncing','synced','requires_review',"
        "'failed','cancelled')",
    )
    for ddl in DDL:
        op.execute(ddl)


def downgrade():
    target = context.get_revision_argument()
    tables = ["sync_window"]
    if target != "0004_commit_parsing":
        tables += ["author_identity", "git_commit", "file_change", "parse_checkpoint"]
    if target not in {"0004_commit_parsing", "0003_repositories"}:
        tables += ["repository"]
    if target not in {"0004_commit_parsing", "0003_repositories", "0002_tasks"}:
        tables += ["async_task", "task_outbox"]
    if target is None:
        tables += ["user", "auth_session", "operation_log"]
    connection = op.get_bind()
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar() for table in tables
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    if connection.execute(
        sa.text("SELECT COUNT(*) FROM repository WHERE sync_root_task_id IS NOT NULL")
    ).scalar():
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    if (
        target != "0004_commit_parsing"
        and connection.execute(
            sa.text("SELECT COUNT(*) FROM repository WHERE parse_root_task_id IS NOT NULL")
        ).scalar()
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    op.drop_table("sync_window")
    op.drop_constraint("fk_repository_sync_root", "repository", type_="foreignkey")
    op.drop_constraint("ck_repository_sync_status", "repository", type_="check")
    op.drop_column("repository", "sync_root_task_id")
    op.drop_column("repository", "sync_status")
