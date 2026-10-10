"""Frozen baseline SZZ schema; refuse populated downgrade before any DDL."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0007_szz"
down_revision = "0006_fix_evidence"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE szz_run (
    root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    repository_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    fix_root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    head_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    storage_key VARCHAR(192) NOT NULL,
    parser_version VARCHAR(64) NOT NULL,
    history_coverage VARCHAR(24) NOT NULL,
    algorithm_version VARCHAR(64) NOT NULL,
    algorithm_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    label_version CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    as_of DATETIME(6) NOT NULL,
    git_version VARCHAR(64),
    plan_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    total BIGINT,
    processed BIGINT NOT NULL DEFAULT '0',
    last_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    PRIMARY KEY (root_task_id),
    CONSTRAINT ck_szz_counts CHECK (processed >= 0 AND (total IS NULL OR total >= processed)),
    FOREIGN KEY(root_task_id) REFERENCES async_task (id) ON DELETE RESTRICT,
    FOREIGN KEY(repository_id) REFERENCES repository (id) ON DELETE RESTRICT,
    FOREIGN KEY(fix_root_task_id) REFERENCES fix_run (root_task_id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE INDEX ix_szz_repository ON szz_run (repository_id, created_at, root_task_id)""",
    """CREATE TABLE szz_item (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    assessment_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    input_json JSON NOT NULL,
    result_json JSON,
    created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    PRIMARY KEY (id),
    CONSTRAINT uq_szz_item UNIQUE (root_task_id, sha),
    FOREIGN KEY(root_task_id) REFERENCES szz_run (root_task_id) ON DELETE RESTRICT,
    FOREIGN KEY(assessment_id) REFERENCES fix_assessment (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    """CREATE TABLE szz_link (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    item_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    ordinal INTEGER NOT NULL,
    evidence JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    PRIMARY KEY (id),
    CONSTRAINT uq_szz_link UNIQUE (item_id, ordinal),
    CONSTRAINT ck_szz_link_ordinal CHECK (ordinal >= 0),
    FOREIGN KEY(item_id) REFERENCES szz_item (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
)


def upgrade():
    op.add_column(
        "repository",
        sa.Column("szz_status", sa.String(24), nullable=False, server_default="pending"),
    )
    op.add_column(
        "repository",
        sa.Column("szz_root_task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin")),
    )
    op.create_foreign_key(
        "fk_repository_szz_root",
        "repository",
        "async_task",
        ["szz_root_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_repository_szz_status",
        "repository",
        "szz_status IN ('pending','queued','tracing','traced','failed','cancelled')",
    )
    for ddl in DDL:
        op.execute(ddl)


def downgrade():
    target = context.get_revision_argument()
    order = [
        "0006_fix_evidence",
        "0005_repository_sync",
        "0004_commit_parsing",
        "0003_repositories",
        "0002_tasks",
        "0001_auth",
    ]
    depth = order.index(target) if target in order else len(order)
    tables = ["szz_link", "szz_item", "szz_run"]
    groups = [
        [],
        ["defect_evidence", "fix_assessment", "issue_observation", "fix_run"],
        ["sync_window"],
        ["file_change", "git_commit", "author_identity", "parse_checkpoint"],
        ["repository"],
        ["async_task", "task_outbox"],
        ["user", "auth_session", "operation_log"],
    ]
    for group in groups[: depth + 1]:
        tables += group
    connection = op.get_bind()
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar() for table in tables
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    for name, threshold in [
        ("szz_root_task_id", 0),
        ("fix_root_task_id", 1),
        ("sync_root_task_id", 2),
        ("parse_root_task_id", 3),
    ]:
        if (
            depth >= threshold
            and connection.execute(
                sa.text(f"SELECT COUNT(*) FROM repository WHERE {name} IS NOT NULL")
            ).scalar()
        ):
            raise RuntimeError("Downgrade refused: affected tables must be empty")
    for table in ("szz_link", "szz_item", "szz_run"):
        op.drop_table(table)
    op.drop_constraint("fk_repository_szz_root", "repository", type_="foreignkey")
    op.drop_constraint("ck_repository_szz_status", "repository", type_="check")
    op.drop_column("repository", "szz_root_task_id")
    op.drop_column("repository", "szz_status")
