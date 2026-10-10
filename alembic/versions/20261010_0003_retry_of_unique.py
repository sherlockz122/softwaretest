"""async_task.retry_of 改为唯一索引：原任务最多一个直接 successor（docs/04 第 10.3 节）。

早期建表只给了普通索引，并发重试可产生多个直接后继；模型已声明 unique=True，
本迁移让存量库收敛。同一条 ALTER 内先删后建，MySQL 在语句结束才校验外键所需索引。

Revision ID: 20261010_0003
Revises: 20261010_0002
Create Date: 2026-10-10
"""

from alembic import op

revision = "20261010_0003"
down_revision = "20261010_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE async_task "
        "DROP INDEX fk_async_task_retry_of, "
        "ADD UNIQUE KEY uq_async_task_retry_of (retry_of)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE async_task "
        "DROP INDEX uq_async_task_retry_of, "
        "ADD KEY fk_async_task_retry_of (retry_of)"
    )
