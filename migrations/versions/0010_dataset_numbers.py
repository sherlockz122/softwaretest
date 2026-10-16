"""Preserve dataset numeric round trips; do not relabel or repair old failed runs."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0010_dataset_numbers"
down_revision = "0009_datasets"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "dataset_member",
        "numeric_json",
        existing_type=mysql.JSON(),
        type_=mysql.MEDIUMTEXT(),
        existing_nullable=False,
    )


def downgrade():
    connection = op.get_bind()
    target = context.get_revision_argument()
    tables = ["dataset_member"]
    if (
        connection.execute(sa.text("SELECT COUNT(*) FROM dataset_version")).scalar()
        or connection.execute(
            sa.text("SELECT COUNT(*) FROM async_task WHERE type='dataset.build'")
        ).scalar()
    ):
        raise RuntimeError("Downgrade refused: dataset versions and tasks must be empty")
    if target != "0009_datasets":
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
            ["dataset_version"],
            ["feature_item", "feature_run"],
            ["szz_link", "szz_item", "szz_run"],
            ["defect_evidence", "fix_assessment", "issue_observation", "fix_run"],
            ["sync_window"],
            ["file_change", "git_commit", "author_identity", "parse_checkpoint"],
            ["repository"],
            ["async_task", "task_outbox"],
            ["user", "auth_session", "operation_log"],
        ]
        tables += [t for g in groups[: depth + 1] for t in g]
        if connection.execute(
            sa.text("SELECT COUNT(*) FROM async_task WHERE type='dataset.build'")
        ).scalar():
            raise RuntimeError("Downgrade refused: dataset tasks must be empty")
    if any(connection.execute(sa.text(f"SELECT COUNT(*) FROM `{t}`")).scalar() for t in tables):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    if target != "0009_datasets":
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
    op.alter_column(
        "dataset_member",
        "numeric_json",
        existing_type=mysql.MEDIUMTEXT(),
        type_=mysql.JSON(),
        existing_nullable=False,
    )
