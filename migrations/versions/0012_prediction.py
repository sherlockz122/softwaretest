"""Prediction tasks and per-candidate results; preflight full downgrade before MySQL DDL."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0012_prediction"
down_revision = "0011_models"
branch_labels = None
depends_on = None


def uuid():
    return mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def digest():
    return mysql.CHAR(64, charset="ascii", collation="ascii_bin")


def upgrade():
    op.create_table(
        "prediction_task",
        sa.Column("id", uuid(), sa.ForeignKey("async_task.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column(
            "repository_id",
            uuid(),
            sa.ForeignKey("repository.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "model_run_id",
            uuid(),
            sa.ForeignKey("model_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("base_sha", digest(), nullable=False),
        sa.Column("base_ref", sa.String(255), nullable=False),
        sa.Column("candidate_kind", sa.String(16), nullable=False),
        sa.Column("candidate_ref", sa.String(255)),
        sa.Column("candidate_sha", digest()),
        sa.Column("candidate_json", mysql.MEDIUMTEXT()),
        sa.Column("feature_version", sa.String(64), nullable=False),
        sa.Column(
            "feature_run_id",
            uuid(),
            sa.ForeignKey("async_task.id", ondelete="RESTRICT"),
        ),
        sa.Column("window_days", sa.Integer()),
        sa.Column("batch_limit", sa.Integer()),
        sa.Column("base_time", mysql.DATETIME(fsp=6)),
        sa.Column("plan_hash", digest()),
        sa.Column("total", sa.Integer()),
        sa.Column("input_digest", digest(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.CheckConstraint(
            "candidate_kind IN ('single_ref','single_diff','batch')",
            name="ck_prediction_kind",
        ),
        sa.CheckConstraint(
            "total IS NULL OR total >= 0",
            name="ck_prediction_total",
        ),
        sa.CheckConstraint(
            "batch_limit IS NULL OR batch_limit > 0",
            name="ck_prediction_batch_limit",
        ),
        sa.CheckConstraint(
            "window_days IS NULL OR (window_days > 0 AND window_days <= 90)",
            name="ck_prediction_window",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "prediction_result",
        sa.Column("id", uuid(), primary_key=True),
        sa.Column(
            "task_id",
            uuid(),
            sa.ForeignKey("prediction_task.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("candidate_ordinal", sa.Integer(), nullable=False),
        sa.Column("candidate_sha", digest(), nullable=False),
        sa.Column("event_time", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("probability", mysql.DECIMAL(7, 6), nullable=False),
        sa.Column("threshold", mysql.DECIMAL(7, 6), nullable=False),
        sa.Column("risk_level", sa.String(8), nullable=False),
        sa.Column("effort", sa.Integer(), nullable=False),
        sa.Column("risk_density", mysql.DECIMAL(7, 6), nullable=False),
        sa.Column("oversized", sa.Boolean(), nullable=False),
        sa.Column("numeric_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("reasons_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("explanation_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("input_json", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("(utc_timestamp(6))"),
        ),
        sa.UniqueConstraint("task_id", "candidate_sha", name="uq_prediction_candidate"),
        sa.CheckConstraint(
            "risk_level IN ('high','medium','low')", name="ck_prediction_level"
        ),
        sa.CheckConstraint(
            "probability >= 0 AND probability <= 1", name="ck_prediction_probability"
        ),
        sa.CheckConstraint("effort >= 0", name="ck_prediction_effort"),
        sa.CheckConstraint(
            "risk_density >= 0 AND risk_density <= 1", name="ck_prediction_density"
        ),
        sa.CheckConstraint("candidate_ordinal >= 0", name="ck_prediction_ordinal"),
        sa.CheckConstraint("oversized IN (0,1)", name="ck_prediction_oversized"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_prediction_queue", "prediction_result", ["task_id", "probability"]
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM async_task "
            "WHERE type IN ('prediction.candidate','prediction.batch')"
        )
    ).scalar():
        raise RuntimeError("Downgrade refused: prediction tasks must be empty")
    for table in ("prediction_result", "prediction_task"):
        if connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar():
            raise RuntimeError("Downgrade refused: affected tables must be empty")
    op.drop_index("ix_prediction_queue", table_name="prediction_result")
    op.drop_table("prediction_result")
    op.drop_table("prediction_task")
