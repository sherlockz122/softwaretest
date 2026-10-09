"""Frozen collection DDL; refuse nonempty affected paths before any MySQL DDL."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0004_commit_parsing"
down_revision = "0003_repositories"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE author_identity (
	id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	identity_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	name_alias VARCHAR(256) NOT NULL,
	email_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	identity_version VARCHAR(64) NOT NULL,
	created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
	PRIMARY KEY (id),
	CONSTRAINT uq_author_identity UNIQUE (identity_key, identity_version)
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE TABLE git_commit (
	id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	repository_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	author_identity_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	author_time DATETIME(6) NOT NULL,
	committer_time DATETIME(6) NOT NULL,
	author_offset INTEGER NOT NULL,
	committer_offset INTEGER NOT NULL,
	message MEDIUMTEXT NOT NULL,
	parents JSON NOT NULL,
	parent_count INTEGER NOT NULL,
	parse_status VARCHAR(32) NOT NULL,
	parser_version VARCHAR(64) NOT NULL,
	created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
	PRIMARY KEY (id),
	CONSTRAINT uq_commit_repository_sha UNIQUE (repository_id, sha),
	CONSTRAINT ck_commit_parents CHECK (parent_count >= 0),
	FOREIGN KEY(repository_id) REFERENCES repository (id) ON DELETE RESTRICT,
	FOREIGN KEY(author_identity_id) REFERENCES author_identity (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE INDEX ix_commit_event ON git_commit (repository_id, committer_time, sha)""",
    """CREATE TABLE file_change (
	id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	commit_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	ordinal INTEGER NOT NULL,
	old_path MEDIUMTEXT,
	new_path MEDIUMTEXT,
	path_bytes_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	old_blob CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	new_blob CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	change_type VARCHAR(16) NOT NULL,
	insertions BIGINT,
	deletions BIGINT,
	old_loc BIGINT,
	is_binary BOOL NOT NULL,
	content_status VARCHAR(32) NOT NULL,
	diff_text MEDIUMTEXT,
	line_numbers JSON,
	parser_version VARCHAR(64) NOT NULL,
	created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
	PRIMARY KEY (id),
	CONSTRAINT uq_file_ordinal UNIQUE (commit_id, ordinal),
	CONSTRAINT ck_file_counts CHECK (ordinal >= 0 AND (insertions IS NULL OR insertions >= 0)
        AND (deletions IS NULL OR deletions >= 0) AND (old_loc IS NULL OR old_loc >= 0)),
	FOREIGN KEY(commit_id) REFERENCES git_commit (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE INDEX ix_file_commit ON file_change (commit_id)""",
    """CREATE TABLE parse_checkpoint (
	root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	repository_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
	head_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	commit_limit BIGINT,
	parser_version VARCHAR(64) NOT NULL,
	plan_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	total BIGINT,
	processed BIGINT NOT NULL DEFAULT '0',
	last_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
	created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
	PRIMARY KEY (root_task_id),
	CONSTRAINT uq_checkpoint_repository UNIQUE (repository_id),
	CONSTRAINT ck_checkpoint_counts CHECK (processed >= 0 AND (total IS NULL OR total >= processed)
        AND (commit_limit IS NULL OR commit_limit > 0)),
	FOREIGN KEY(root_task_id) REFERENCES async_task (id) ON DELETE RESTRICT,
	FOREIGN KEY(repository_id) REFERENCES repository (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
)


def upgrade():
    op.add_column(
        "repository",
        sa.Column("parse_status", sa.String(24), nullable=False, server_default="pending"),
    )
    op.add_column(
        "repository",
        sa.Column("parse_root_task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin")),
    )
    op.create_foreign_key(
        "fk_repository_parse_root",
        "repository",
        "async_task",
        ["parse_root_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_repository_parse_status",
        "repository",
        "parse_status IN ('pending','queued','parsing','parsed','failed','cancelled')",
    )
    for ddl in DDL:
        op.execute(ddl)


def downgrade():
    target = context.get_revision_argument()
    tables = ["author_identity", "git_commit", "file_change", "parse_checkpoint"]
    if target != "0003_repositories":
        tables += ["repository"]
    if target not in {"0003_repositories", "0002_tasks"}:
        tables += ["async_task", "task_outbox"]
    if target is None:
        tables += ["user", "auth_session", "operation_log"]
    connection = op.get_bind()
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar() for table in tables
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    # Even with empty child tables, refuse loss of an established parse window.
    if connection.execute(
        sa.text("SELECT COUNT(*) FROM repository WHERE parse_root_task_id IS NOT NULL")
    ).scalar():
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    for name in ("parse_checkpoint", "file_change", "git_commit", "author_identity"):
        op.drop_table(name)
    op.drop_constraint("fk_repository_parse_root", "repository", type_="foreignkey")
    op.drop_constraint("ck_repository_parse_status", "repository", type_="check")
    op.drop_column("repository", "parse_root_task_id")
    op.drop_column("repository", "parse_status")
