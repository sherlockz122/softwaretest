"""时间列统一 DATETIME(6)：修正早期开发库按整秒建表导致的状态迁移精度偏差。

docs/04 第 10.4 节要求时间为 UTC DATETIME(6)；任务租约/退避/心跳依赖微秒精度。
alembic alter_column 会丢失精度与可空性，这里用显式 DDL 保持 NOT NULL 语义；
对已按 DATETIME(6) 建表的库为等价 MODIFY，保留全部数据。

Revision ID: 20261010_0002
Revises: 20261009_0001
Create Date: 2026-10-10
"""

from alembic import op

revision = "20261010_0002"
down_revision = "20261009_0001"
branch_labels = None
depends_on = None

DATETIME_COLUMNS = {
    "user": {"required": ["created_at", "updated_at"], "nullable": []},
    "auth_session": {
        "required": ["expires_at", "created_at", "updated_at"],
        "nullable": ["revoked_at"],
    },
    "async_task": {
        "required": ["queued_deadline", "created_at", "updated_at"],
        "nullable": ["heartbeat_at", "lease_until", "started_at", "finished_at"],
    },
    "task_outbox": {
        "required": ["next_attempt_at", "created_at", "updated_at"],
        "nullable": ["lease_until", "sent_at"],
    },
    "operation_log": {"required": ["created_at"], "nullable": []},
}


def _modify(precision: str) -> None:
    for table, columns in DATETIME_COLUMNS.items():
        for column in columns["required"]:
            op.execute(f"ALTER TABLE {table} MODIFY {column} DATETIME{precision} NOT NULL")
        for column in columns["nullable"]:
            op.execute(f"ALTER TABLE {table} MODIFY {column} DATETIME{precision} NULL")


def upgrade() -> None:
    _modify("(6)")


def downgrade() -> None:
    _modify("")
