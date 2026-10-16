"""Purpose-separated datasets; refuse the full downgrade before any MySQL DDL."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0009_datasets"
down_revision = "0008_features"
branch_labels = None
depends_on = None


def uuid():
    return mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def digest():
    return mysql.CHAR(64, charset="ascii", collation="ascii_bin")


def upgrade():
    op.create_table(
        "dataset_version",
        sa.Column(
            "id", uuid(), sa.ForeignKey("async_task.id", ondelete="RESTRICT"), primary_key=True
        ),
        sa.Column(
            "latest_task_id",
            uuid(),
            sa.ForeignKey("async_task.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("asset_id", digest(), nullable=False),
        sa.Column("source_hash", digest(), nullable=False),
        sa.Column("policy_json", mysql.JSON(), nullable=False),
        sa.Column("plan_hash", digest()),
        sa.Column("plan_json", mysql.JSON()),
        sa.Column("output_json", mysql.JSON()),
        sa.Column("total", sa.BigInteger()),
        sa.Column("processed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.CheckConstraint(
            "processed >= 0 AND (total IS NULL OR total >= processed)", name="ck_dataset_counts"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_dataset_created", "dataset_version", ["created_at", "id"])
    op.create_table(
        "dataset_member",
        sa.Column(
            "dataset_id",
            uuid(),
            sa.ForeignKey("dataset_version.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("canonical_repository", sa.String(160), nullable=False),
        sa.Column(
            "commit_id", mysql.CHAR(40, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("event_epoch", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.Integer(), nullable=False),
        sa.Column("partition", sa.String(16), nullable=False),
        sa.Column("numeric_json", mysql.JSON(), nullable=False),
        sa.Column("source_json", mysql.JSON(), nullable=False),
        sa.UniqueConstraint(
            "dataset_id", "canonical_repository", "commit_id", name="uq_dataset_identity"
        ),
        sa.CheckConstraint("ordinal >= 0 AND label IN (0,1)", name="ck_dataset_member_values"),
        sa.CheckConstraint(
            "`partition` IN ('train','validation','test')", name="ck_dataset_partition"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_dataset_partition", "dataset_member", ["dataset_id", "partition", "ordinal"]
    )


def downgrade():
    connection = op.get_bind()
    target = context.get_revision_argument()
    order = [
        "0008_features",
        "0007_szz",
        "0006_fix_evidence",
        "0005_repository_sync",
        "0004_commit_parsing",
        "0003_repositories",
        "0002_tasks",
        "0001_auth",
    ]
    depth = order.index(target) if target in order else len(order)
    groups = [
        [],
        ["feature_item", "feature_run"],
        ["szz_link", "szz_item", "szz_run"],
        ["defect_evidence", "fix_assessment", "issue_observation", "fix_run"],
        ["sync_window"],
        ["file_change", "git_commit", "author_identity", "parse_checkpoint"],
        ["repository"],
        ["async_task", "task_outbox"],
        ["user", "auth_session", "operation_log"],
    ]
    tables = ["dataset_member", "dataset_version"] + [t for g in groups[: depth + 1] for t in g]
    if any(connection.execute(sa.text(f"SELECT COUNT(*) FROM `{t}`")).scalar() for t in tables):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    if connection.execute(
        sa.text("SELECT COUNT(*) FROM async_task WHERE type = 'dataset.build'")
    ).scalar():
        raise RuntimeError("Downgrade refused: dataset tasks must be empty")
    for field, threshold in [
        ("feature_root_task_id", 1),
        ("szz_root_task_id", 2),
        ("fix_root_task_id", 3),
        ("sync_root_task_id", 4),
        ("parse_root_task_id", 5),
    ]:
        if (
            depth >= threshold
            and connection.execute(
                sa.text(f"SELECT COUNT(*) FROM repository WHERE {field} IS NOT NULL")
            ).scalar()
        ):
            raise RuntimeError("Downgrade refused: affected references must be empty")
    op.drop_table("dataset_member")
    op.drop_table("dataset_version")
