"""Dataset observation bindings: repository rounds fixed per dataset build."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import context, op

revision = "0014_dataset_observation"
down_revision = "0013_model_pool"
branch_labels = None
depends_on = None


def uuid():
    return mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def digest():
    return mysql.CHAR(64, charset="ascii", collation="ascii_bin")


def upgrade():
    op.create_table(
        "dataset_observation",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column(
            "dataset_version_id",
            uuid(),
            sa.ForeignKey("dataset_version.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "repository_id",
            uuid(),
            sa.ForeignKey("repository.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "feature_run_id",
            uuid(),
            sa.ForeignKey("feature_run.root_task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("szz_run_ids_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("binding_hash", digest(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.UniqueConstraint("dataset_version_id", name="uq_dataset_observation_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_dataset_observation_binding",
        "dataset_observation",
        ["binding_hash", "created_at", "id"],
    )


def downgrade():
    connection = op.get_bind()
    target = context.get_revision_argument()
    if connection.execute(sa.text("SELECT COUNT(*) FROM dataset_observation")).scalar():
        raise RuntimeError("Downgrade refused: dataset_observation must be empty")
    # MySQL 的 DDL 隐式提交：先删本迁移的表、再让下游迁移拒绝，会留下「表已删、
    # 版本没动」的半截状态，复跑时本迁移会找不到自己的表。因此与 0011/0012 同款
    # 做法——动任何 DDL 之前，把下游链上到目标为止的表全部预检一遍，非空即拒绝。
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
    downstream = [t for group in groups[: depth + 1] for t in group]
    if (
        depth >= 1
        and connection.execute(
            sa.text("SELECT COUNT(*) FROM async_task WHERE type='dataset.build'")
        ).scalar()
    ):
        raise RuntimeError("Downgrade refused: dataset tasks must be empty")
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
        for table in downstream
    ):
        raise RuntimeError("Downgrade refused: downstream tables must be empty")
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
    op.drop_index("ix_dataset_observation_binding", table_name="dataset_observation")
    op.drop_table("dataset_observation")
