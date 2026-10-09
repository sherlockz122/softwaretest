"""Frozen MySQL task/outbox DDL; independent of subsequent ORM changes."""

import sqlalchemy as sa
from alembic import context, op

revision = "0002_tasks"
down_revision = "0001_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text("""
CREATE TABLE async_task (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    actor_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    type VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    scope_key VARCHAR(192) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    idempotency_key VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload JSON NOT NULL,
    status VARCHAR(24) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    progress DECIMAL(5, 2) NOT NULL DEFAULT '0',
    processed BIGINT NOT NULL DEFAULT '0',
    total BIGINT,
    heartbeat_at DATETIME(6),
    lease_until DATETIME(6),
    execution_token CHAR(36) CHARACTER SET ascii COLLATE ascii_bin,
    started_at DATETIME(6),
    finished_at DATETIME(6),
    retry_of CHAR(36) CHARACTER SET ascii COLLATE ascii_bin,
    root_task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT '0',
    queued_deadline DATETIME(6) NOT NULL,
    result_json JSON,
    error_code VARCHAR(64),
    error_message VARCHAR(512),
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    version BIGINT NOT NULL DEFAULT '0',
    updated_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    PRIMARY KEY (id),
    CONSTRAINT uq_task_request UNIQUE (type, scope_key, idempotency_key),
    CONSTRAINT ck_task_status CHECK (
        status IN ('queued','running','cancel_requested','succeeded','failed','cancelled')),
    CONSTRAINT ck_task_progress CHECK ( progress BETWEEN 0 AND 100),
    CONSTRAINT ck_task_counts CHECK ( processed >= 0 AND (total IS NULL OR total >= 0)),
    CONSTRAINT ck_task_versions CHECK ( retry_count >= 0 AND version >= 0),
    CONSTRAINT ck_task_payload_size CHECK ( JSON_STORAGE_SIZE(payload) <= 16384),
    CONSTRAINT ck_task_result_size CHECK (
        result_json IS NULL OR JSON_STORAGE_SIZE(result_json) <= 4096),
    FOREIGN KEY(actor_id) REFERENCES user (id) ON DELETE RESTRICT,
    UNIQUE (retry_of),
    FOREIGN KEY(retry_of) REFERENCES async_task (id) ON DELETE RESTRICT,
    FOREIGN KEY(root_task_id) REFERENCES async_task (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4
""")
    )
    op.execute(sa.text("CREATE INDEX ix_task_created_id ON async_task (created_at, id)"))
    op.execute(sa.text("CREATE INDEX ix_task_lease ON async_task (status, lease_until)"))
    op.execute(
        sa.text("CREATE INDEX ix_task_queue_deadline ON async_task (status, queued_deadline)")
    )
    op.execute(
        sa.text("""
CREATE TABLE task_outbox (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    event_type VARCHAR(32) NOT NULL DEFAULT 'execute',
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT '0',
    next_attempt_at DATETIME(6) NOT NULL,
    lease_until DATETIME(6),
    delivery_token CHAR(36) CHARACTER SET ascii COLLATE ascii_bin,
    sent_at DATETIME(6),
    last_error_code VARCHAR(64),
    created_at DATETIME(6) NOT NULL DEFAULT (utc_timestamp(6)),
    PRIMARY KEY (id),
    CONSTRAINT uq_outbox_event UNIQUE (task_id, event_type),
    CONSTRAINT ck_outbox_status CHECK ( status IN ('pending','dispatching','sent','dead')),
    CONSTRAINT ck_outbox_attempts CHECK ( attempts >= 0),
    FOREIGN KEY(task_id) REFERENCES async_task (id) ON DELETE RESTRICT
)ENGINE=InnoDB CHARSET=utf8mb4
""")
    )
    op.execute(sa.text("CREATE INDEX ix_outbox_due ON task_outbox (status, next_attempt_at)"))
    op.execute(sa.text("CREATE INDEX ix_outbox_lease ON task_outbox (status, lease_until)"))


def downgrade():
    connection = op.get_bind()
    tables = ["async_task", "task_outbox"]
    # MySQL DDL commits implicitly. Preflight the full path before dropping tasks,
    # otherwise a later authentication guard could leave the revision inconsistent.
    if context.get_revision_argument() is None:
        tables += ["user", "auth_session", "operation_log"]
    if any(
        connection.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar() for table in tables
    ):
        raise RuntimeError("Downgrade refused: affected tables must be empty")
    op.drop_table("task_outbox")
    op.drop_table("async_task")
