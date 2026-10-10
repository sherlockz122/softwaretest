"""恢复协调器：租约过期/队列超时条件写 failed，为可重试基础设施失败创建 successor（第 10.5 节）。"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.models import AsyncTask
from packages.platform.config import Settings
from packages.security.tokens import as_utc, now_utc
from packages.tasks.domain import RETRYABLE_INFRA_ERRORS, add_audit_log
from packages.tasks.service import add_successor

logger = logging.getLogger("defectguard")


def _fail_conditionally(session: Session, task_id: str, now, *, status: str,
                        error_code: str, error_message: str) -> bool:
    """在行锁内重查条件后写 failed；返回是否写入。"""
    task = session.execute(
        select(AsyncTask).where(AsyncTask.id == task_id).with_for_update()
    ).scalar_one_or_none()
    if task is None or task.status != status:
        session.commit()
        return False
    if status == "running" and as_utc(task.lease_until) >= now:
        session.commit()
        return False
    task.status = "failed"
    task.stage = "failed"
    task.error_code = error_code
    task.error_message = error_message
    task.finished_at = now
    task.execution_token = None
    task.lease_until = None
    task.version += 1
    add_audit_log(session, actor_id=task.actor_id, action="task.fail", task=task,
                  result="succeeded", request_id=task.request_id)
    session.commit()
    return True


def _complete_cancel(session: Session, task_id: str, now) -> bool:
    """cancel_requested 且租约过期的僵尸任务：执行者已消失，接受取消完成。"""
    task = session.execute(
        select(AsyncTask).where(AsyncTask.id == task_id).with_for_update()
    ).scalar_one_or_none()
    if task is None or task.status != "cancel_requested":
        session.commit()
        return False
    if as_utc(task.lease_until) >= now:
        session.commit()
        return False
    task.status = "cancelled"
    task.stage = "cancelled"
    task.finished_at = now
    task.execution_token = None
    task.lease_until = None
    task.version += 1
    add_audit_log(session, actor_id=task.actor_id, action="task.cancel", task=task,
                  result="succeeded", request_id=task.request_id)
    session.commit()
    return True


def _recover_if_allowed(session: Session, settings: Settings, task_id: str) -> bool:
    """为可重试失败且无 successor 的任务创建 successor；自动恢复链有上限。"""
    task = session.execute(
        select(AsyncTask).where(AsyncTask.id == task_id).with_for_update()
    ).scalar_one_or_none()
    if task is None:
        session.commit()
        return False
    if (
        task.status != "failed"
        or task.error_code not in RETRYABLE_INFRA_ERRORS
        or task.retry_count >= settings.task_auto_recovery_limit
    ):
        session.commit()
        return False
    existing = session.execute(
        select(AsyncTask.id).where(AsyncTask.retry_of == task.id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        session.commit()
        return False
    successor = add_successor(
        session, settings, task, actor_id=task.actor_id,
        idempotency_key=f"auto:{task.id}", request_id=task.request_id,
        action="task.recover",
    )
    session.commit()
    logger.info("TASK_RECOVERED task_id=%s successor_of=%s", successor.id, task.id)
    return True


def run_coordinator_cycle(session_factory, settings: Settings) -> dict:
    """单轮协调扫描：先条件写失败/取消，再为可重试失败补 successor。"""
    now = now_utc()
    stats = {"lease_expired": 0, "queue_timeout": 0, "cancel_completed": 0,
             "recovered": 0}
    with session_factory() as session:
        queue_ids = session.execute(
            select(AsyncTask.id).where(
                AsyncTask.status == "queued", AsyncTask.queued_deadline < now
            )
        ).scalars().all()
        lease_ids = session.execute(
            select(AsyncTask.id).where(
                AsyncTask.status == "running", AsyncTask.lease_until < now
            )
        ).scalars().all()
        cancel_ids = session.execute(
            select(AsyncTask.id).where(
                AsyncTask.status == "cancel_requested", AsyncTask.lease_until < now
            )
        ).scalars().all()
    for task_id in queue_ids:
        with session_factory() as session:
            if _fail_conditionally(session, task_id, now, status="queued",
                                   error_code="TASK_QUEUE_TIMEOUT",
                                   error_message="任务排队超时"):
                stats["queue_timeout"] += 1
    for task_id in lease_ids:
        with session_factory() as session:
            if _fail_conditionally(session, task_id, now, status="running",
                                   error_code="TASK_LEASE_EXPIRED",
                                   error_message="任务执行租约过期"):
                stats["lease_expired"] += 1
    for task_id in cancel_ids:
        with session_factory() as session:
            if _complete_cancel(session, task_id, now):
                stats["cancel_completed"] += 1
    with session_factory() as session:
        retryable_ids = session.execute(
            select(AsyncTask.id).where(
                AsyncTask.status == "failed",
                AsyncTask.error_code.in_(RETRYABLE_INFRA_ERRORS),
            )
        ).scalars().all()
    for task_id in retryable_ids:
        with session_factory() as session:
            if _recover_if_allowed(session, settings, task_id):
                stats["recovered"] += 1
    return stats
