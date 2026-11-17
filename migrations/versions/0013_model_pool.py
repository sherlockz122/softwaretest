"""Model pool entries: sealed estimator params with training contracts."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import context, op

revision = "0013_model_pool"
down_revision = "0012_prediction"
branch_labels = None
depends_on = None


def uuid():
    return mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def digest():
    return mysql.CHAR(64, charset="ascii", collation="ascii_bin")


def upgrade():
    op.create_table(
        "model_pool",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "source_kind",
            mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "source_run_id",
            uuid(),
            sa.ForeignKey("model_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "owner_id",
            uuid(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("plugin", sa.String(16), nullable=False),
        sa.Column("params_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("features_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("forms_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("code_sha", digest(), nullable=False),
        sa.Column("artifact_uri", sa.String(256), nullable=False),
        sa.Column("artifact_sha256", digest(), nullable=False),
        sa.Column("artifact_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.CheckConstraint(
            "source_kind IN ('preset','run') AND status IN ('active','removed')",
            name="ck_model_pool_kind_status",
        ),
        sa.CheckConstraint("artifact_size >= 0", name="ck_model_pool_artifact_size"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_model_pool_kind_created", "model_pool", ["source_kind", "created_at", "id"])


def downgrade():
    connection = op.get_bind()
    target = context.get_revision_argument()
    if connection.execute(sa.text("SELECT COUNT(*) FROM model_pool")).scalar():
        raise RuntimeError("Downgrade refused: model_pool must be empty")
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
    op.drop_index("ix_model_pool_kind_created", table_name="model_pool")
    op.drop_table("model_pool")
