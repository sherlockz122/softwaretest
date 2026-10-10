"""Outbox 投递器：短事务领取租约、释放 DB 锁后发送、token 条件写回（docs/04 第 10.5 节）。"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from packages.domain.models import AsyncTask, TaskOutbox
from packages.platform.config import Settings
from packages.security.tokens import now_utc
from packages.tasks.domain import EXECUTE_TASK_NAME, delivery_delay_seconds

logger = logging.getLogger("defectguard")


def _claim_next(session: Session, settings: Settings) -> tuple[TaskOutbox, str] | None:
    """领取一个到期 pending 或租约过期的 dispatching 行，返回 (outbox, delivery_token)。"""
    now = now_utc()
    row = session.execute(
        select(TaskOutbox)
        .where(
            or_(
                and_(TaskOutbox.status == "pending", TaskOutbox.next_attempt_at <= now),
                and_(TaskOutbox.status == "dispatching", TaskOutbox.lease_until < now),
            )
        )
        .with_for_update(skip_locked=True)
        .order_by(TaskOutbox.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        session.commit()
        return None
    token = str(uuid.uuid4())
    row.attempts += 1
    row.status = "dispatching"
    row.delivery_token = token
    row.lease_until = now + timedelta(seconds=settings.task_delivery_lease_seconds)
    row.next_attempt_at = now + timedelta(seconds=delivery_delay_seconds(row.attempts))
    row.last_error_code = None
    session.commit()
    return row, token


def _abandon_if_task_not_queued(session: Session, row: TaskOutbox, token: str) -> bool:
    """任务不再 queued（取消/终态/被恢复）时直接终结投递，不发送消息。"""
    status = session.execute(
        select(AsyncTask.status).where(AsyncTask.id == row.task_id)
    ).scalar_one()
    if status != "queued":
        session.execute(
            update(TaskOutbox)
            .where(TaskOutbox.id == row.id, TaskOutbox.delivery_token == token)
            .values(status="dead", lease_until=None, last_error_code="TASK_NOT_QUEUED")
        )
        session.commit()
        return True
    session.commit()
    return False


def _write_back(session: Session, settings: Settings, row: TaskOutbox, token: str, *,
                sent: bool, error_code: str | None) -> None:
    """成功写 sent；失败按剩余尝试写 pending 或 dead（token 条件，不覆盖他人领取）。"""
    now = now_utc()
    if sent:
        session.execute(
            update(TaskOutbox)
            .where(
                TaskOutbox.id == row.id,
                TaskOutbox.delivery_token == token,
                TaskOutbox.status == "dispatching",
            )
            .values(status="sent", sent_at=now, lease_until=None, next_attempt_at=now)
        )
        session.commit()
        return
    dead = row.attempts >= settings.task_dispatch_attempts
    session.execute(
        update(TaskOutbox)
        .where(
            TaskOutbox.id == row.id,
            TaskOutbox.delivery_token == token,
            TaskOutbox.status == "dispatching",
        )
        .values(
            status="dead" if dead else "pending",
            lease_until=None,
            delivery_token=None,
            last_error_code=error_code or "TASK_DISPATCH_ERROR",
            next_attempt_at=now + timedelta(seconds=delivery_delay_seconds(row.attempts))
            if not dead
            else now,
        )
    )
    if dead:
        # 只对仍 queued 的任务写 failed；running/succeeded 等由自身状态决定。
        session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == row.task_id, AsyncTask.status == "queued")
            .values(
                status="failed",
                stage="failed",
                error_code="TASK_DISPATCH_FAILED",
                error_message="任务投递失败",
                finished_at=now,
                version=AsyncTask.version + 1,
            )
        )
    session.commit()


def run_dispatch_cycle(session_factory, settings: Settings, celery_app) -> dict:
    """单轮投递：领取一个到期 outbox 行并尝试发送。返回本轮统计。"""
    stats = {"claimed": 0, "sent": 0, "failed": 0}
    with session_factory() as session:
        claimed = _claim_next(session, settings)
        if claimed is None:
            return stats
        row, token = claimed
        stats["claimed"] = 1
        if _abandon_if_task_not_queued(session, row, token):
            return stats
    try:
        celery_app.send_task(
            EXECUTE_TASK_NAME,
            args=[row.task_id],
            retry=False,
        )
    except Exception:  # Redis 不可用等：写回失败，退避重试
        logger.warning("TASK_DISPATCH_ERROR task_id=%s attempt=%s", row.task_id, row.attempts)
        with session_factory() as session:
            _write_back(session, settings, row, token, sent=False,
                        error_code="TASK_DISPATCH_ERROR")
        stats["failed"] = 1
        return stats
    with session_factory() as session:
        _write_back(session, settings, row, token, sent=True, error_code=None)
    stats["sent"] = 1
    return stats
