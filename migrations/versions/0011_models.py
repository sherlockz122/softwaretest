"""Research model roots and artifacts; preflight full downgrade before MySQL DDL."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0011_models"
down_revision = "0010_dataset_numbers"
branch_labels = None
depends_on = None


def uuid():
    return mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def digest():
    return mysql.CHAR(64, charset="ascii", collation="ascii_bin")


def upgrade():
    op.create_table(
        "model_run",
        sa.Column(
            "id", uuid(), sa.ForeignKey("async_task.id", ondelete="RESTRICT"), primary_key=True
        ),
        sa.Column(
            "latest_task_id",
            uuid(),
            sa.ForeignKey("async_task.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            uuid(),
            sa.ForeignKey("dataset_version.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plugin", sa.String(16), nullable=False),
        sa.Column("params_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("code_sha", digest(), nullable=False),
        sa.Column("snapshot_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("metrics_json", mysql.MEDIUMTEXT()),
        sa.Column("output_json", mysql.MEDIUMTEXT()),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.CheckConstraint("seed >= 0", name="ck_model_seed"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_model_created", "model_run", ["created_at", "id"])
    op.create_table(
        "model_artifact",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column(
            "model_run_id",
            uuid(),
            sa.ForeignKey("model_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("uri", sa.String(256), nullable=False),
        sa.Column("sha256", digest(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("model_run_id", "kind", name="uq_model_artifact_kind"),
        sa.CheckConstraint("size >= 0", name="ck_model_artifact_size"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade():
    connection = op.get_bind()
    target = context.get_revision_argument()
    tables = ["model_artifact", "model_run"]
    if connection.execute(
        sa.text("SELECT COUNT(*) FROM async_task WHERE type='model.train'")
    ).scalar():
        raise RuntimeError("Downgrade refused: model tasks must be empty")
    order = [
        "0010_dataset_numbers",
        "0009_datasets",
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
        ["dataset_member", "dataset_version"],
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
    tables += [t for group in groups[: depth + 1] for t in group]
    if (
        depth >= 1
        and connection.execute(
            sa.text("SELECT COUNT(*) FROM async_task WHERE type='dataset.build'")
        ).scalar()
    ):
        raise RuntimeError("Downgrade refused: dataset tasks must be empty")
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar() for table in tables
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    for field, threshold in [
        ("feature_root_task_id", 3),
        ("szz_root_task_id", 4),
        ("fix_root_task_id", 5),
        ("sync_root_task_id", 6),
        ("parse_root_task_id", 7),
    ]:
        if (
            depth >= threshold
            and connection.execute(
                sa.text(f"SELECT COUNT(*) FROM repository WHERE {field} IS NOT NULL")
            ).scalar()
        ):
            raise RuntimeError("Downgrade refused: affected references must be empty")
    op.drop_table("model_artifact")
    op.drop_table("model_run")
