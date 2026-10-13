"""Frozen feature schema; validate the entire downgrade path before MySQL DDL."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import context, op

revision = "0008_features"
down_revision = "0007_szz"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE feature_run (
    root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    repository_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    fix_root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin,
    head_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    storage_key VARCHAR(192) NOT NULL,
    parser_version VARCHAR(64) NOT NULL,
    identity_version VARCHAR(64) NOT NULL,
    history_coverage VARCHAR(24) NOT NULL,
    feature_version VARCHAR(64) NOT NULL,
    rule_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    purpose VARCHAR(24) NOT NULL,
    fix_json JSON NOT NULL,
    fix_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    input_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    quality_json JSON,
    git_version VARCHAR(64),
    plan_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    total BIGINT,
    processed BIGINT NOT NULL DEFAULT '0',
    last_sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    PRIMARY KEY (root_task_id),
    CONSTRAINT ck_feature_counts CHECK (processed >= 0 AND (total IS NULL OR total >= processed)),
    CONSTRAINT ck_feature_purpose CHECK (purpose IN ('historical','candidate')),
    FOREIGN KEY(root_task_id) REFERENCES async_task (id) ON DELETE RESTRICT,
    FOREIGN KEY(repository_id) REFERENCES repository (id) ON DELETE RESTRICT,
    FOREIGN KEY(fix_root_task_id) REFERENCES fix_run (root_task_id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
    "CREATE INDEX ix_feature_repository ON feature_run (repository_id, created_at, root_task_id)",
    """CREATE TABLE feature_item (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    ordinal INTEGER NOT NULL,
    sha CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    input_json JSON NOT NULL,
    result_json JSON,
    created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    PRIMARY KEY (id),
    CONSTRAINT uq_feature_item_sha UNIQUE (root_task_id, sha),
    CONSTRAINT uq_feature_item_ordinal UNIQUE (root_task_id, ordinal),
    CONSTRAINT ck_feature_item_ordinal CHECK (ordinal >= 0),
    FOREIGN KEY(root_task_id) REFERENCES feature_run (root_task_id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4""",
)


def upgrade():
    op.add_column(
        "repository",
        sa.Column("feature_status", sa.String(24), nullable=False, server_default="pending"),
    )
    op.add_column(
        "repository",
        sa.Column("feature_root_task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin")),
    )
    op.create_foreign_key(
        "fk_repository_feature_root",
        "repository",
        "async_task",
        ["feature_root_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_repository_feature_status",
        "repository",
        "feature_status IN ('pending','queued','extracting','extracted','failed','cancelled')",
    )
    for ddl in DDL:
        op.execute(ddl)


def downgrade():
    order = [
        "0007_szz",
        "0006_fix_evidence",
        "0005_repository_sync",
        "0004_commit_parsing",
        "0003_repositories",
        "0002_tasks",
        "0001_auth",
    ]
    target = context.get_revision_argument()
    depth = order.index(target) if target in order else len(order)
    tables = ["feature_item", "feature_run"]
    groups = [
        [],
        ["szz_link", "szz_item", "szz_run"],
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
        ("feature_root_task_id", 0),
        ("szz_root_task_id", 1),
        ("fix_root_task_id", 2),
        ("sync_root_task_id", 3),
        ("parse_root_task_id", 4),
    ]:
        if (
            depth >= threshold
            and connection.execute(
                sa.text(f"SELECT COUNT(*) FROM repository WHERE {name} IS NOT NULL")
            ).scalar()
        ):
            raise RuntimeError("Downgrade refused: affected tables must be empty")
    for table in ("feature_item", "feature_run"):
        op.drop_table(table)
    op.drop_constraint("fk_repository_feature_root", "repository", type_="foreignkey")
    op.drop_constraint("ck_repository_feature_status", "repository", type_="check")
    op.drop_column("repository", "feature_root_task_id")
    op.drop_column("repository", "feature_status")
