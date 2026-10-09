"""Frozen Fix evidence and review schema; refuse destructive populated downgrade."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0006_fix_evidence"
down_revision = "0005_repository_sync"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE fix_run (
        root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        repository_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        head_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
        storage_key VARCHAR(192) NOT NULL,
        parser_version VARCHAR(64) NOT NULL,
        history_coverage VARCHAR(24) NOT NULL,
        rule_version VARCHAR(64) NOT NULL,
        rule_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        include_medium BOOL NOT NULL,
        plan_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
        total BIGINT,
        processed BIGINT NOT NULL DEFAULT '0',
        last_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
        created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
        PRIMARY KEY (root_task_id),
        CONSTRAINT ck_fix_counts CHECK (processed >= 0 AND (total IS NULL OR total >=
processed)),
        FOREIGN KEY(root_task_id) REFERENCES async_task (id) ON DELETE RESTRICT,
        FOREIGN KEY(repository_id) REFERENCES repository (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE INDEX ix_fix_repository ON fix_run (repository_id, created_at, root_task_id)""",
    """CREATE TABLE fix_assessment (
        id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        commit_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        rule_candidate BOOL NOT NULL,
        disposition VARCHAR(32) NOT NULL,
        content_complete BOOL NOT NULL,
        review_status VARCHAR(24) NOT NULL DEFAULT 'unreviewed',
        review_revision INTEGER NOT NULL DEFAULT '0',
        review_actor_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin,
        review_note VARCHAR(300),
        reviewed_at DATETIME(6),
        created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
        PRIMARY KEY (id),
        CONSTRAINT uq_fix_assessment UNIQUE (root_task_id, commit_id),
        CONSTRAINT ck_fix_review CHECK (review_status IN ('unreviewed','confirmed','rejected')
AND review_revision >= 0),
        FOREIGN KEY(root_task_id) REFERENCES fix_run (root_task_id) ON DELETE RESTRICT,
        FOREIGN KEY(commit_id) REFERENCES git_commit (id) ON DELETE RESTRICT,
        FOREIGN KEY(review_actor_id) REFERENCES user (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE INDEX ix_fix_assessment_run ON fix_assessment (root_task_id, sha)""",
    """CREATE TABLE defect_evidence (
        id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        assessment_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        fix_commit_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        ordinal INTEGER NOT NULL,
        type VARCHAR(32) NOT NULL,
        value VARCHAR(300) NOT NULL,
        confidence VARCHAR(16) NOT NULL,
        rule_version VARCHAR(64) NOT NULL,
        source JSON NOT NULL,
        created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
        PRIMARY KEY (id),
        CONSTRAINT uq_defect_evidence UNIQUE (assessment_id, ordinal),
        CONSTRAINT ck_evidence_confidence CHECK (ordinal >= 0 AND confidence IN
('high','medium','low')),
        FOREIGN KEY(assessment_id) REFERENCES fix_assessment (id) ON DELETE RESTRICT,
        FOREIGN KEY(fix_commit_id) REFERENCES git_commit (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE TABLE issue_observation (
        root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        number BIGINT NOT NULL,
        snapshot JSON NOT NULL,
        created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
        PRIMARY KEY (root_task_id, number),
        FOREIGN KEY(root_task_id) REFERENCES fix_run (root_task_id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
)


def upgrade():
    op.add_column(
        "repository",
        sa.Column("fix_status", sa.String(24), nullable=False, server_default="pending"),
    )
    op.add_column(
        "repository",
        sa.Column("fix_root_task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin")),
    )
    op.create_foreign_key(
        "fk_repository_fix_root",
        "repository",
        "async_task",
        ["fix_root_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_repository_fix_status",
        "repository",
        "fix_status IN ('pending','queued','detecting','detected','failed','cancelled')",
    )
    for ddl in DDL:
        op.execute(ddl)


def downgrade():
    target = context.get_revision_argument()
    tables = ["defect_evidence", "fix_assessment", "issue_observation", "fix_run"]
    if target != "0005_repository_sync":
        tables += ["sync_window"]
    if target not in {"0005_repository_sync", "0004_commit_parsing"}:
        tables += ["file_change", "git_commit", "author_identity", "parse_checkpoint"]
    if target not in {"0005_repository_sync", "0004_commit_parsing", "0003_repositories"}:
        tables += ["repository"]
    if target not in {
        "0005_repository_sync",
        "0004_commit_parsing",
        "0003_repositories",
        "0002_tasks",
    }:
        tables += ["async_task", "task_outbox"]
    if target is None:
        tables += ["user", "auth_session", "operation_log"]
    connection = op.get_bind()
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar() for table in tables
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    if connection.execute(
        sa.text("SELECT COUNT(*) FROM repository WHERE fix_root_task_id IS NOT NULL")
    ).scalar():
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    for name, previous in (
        ("sync_root_task_id", "0005_repository_sync"),
        ("parse_root_task_id", "0004_commit_parsing"),
    ):
        if (
            target not in {"0005_repository_sync", previous}
            and connection.execute(
                sa.text(f"SELECT COUNT(*) FROM repository WHERE {name} IS NOT NULL")
            ).scalar()
        ):
            raise RuntimeError("Downgrade refused: affected tables must be empty")
    for table in ("defect_evidence", "fix_assessment", "issue_observation", "fix_run"):
        op.drop_table(table)
    op.drop_constraint("fk_repository_fix_root", "repository", type_="foreignkey")
    op.drop_constraint("ck_repository_fix_status", "repository", type_="check")
    op.drop_column("repository", "fix_root_task_id")
    op.drop_column("repository", "fix_status")
