"""任务创建/取消/重试与查询：业务任务、outbox 与审计在同一 MySQL 事务保存。"""

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.domain.models import AsyncTask, TaskOutbox, User
from packages.platform.config import Settings
from packages.security.tokens import now_utc
from packages.tasks.domain import (
    TaskRejection,
    add_audit_log,
    canonical_payload_hash,
    fetch_task_or_none,
    serialize_task,
)

NOT_FOUND = (404, "TASK_NOT_FOUND", "任务不存在")
STATE_CONFLICT = (409, "TASK_STATE_CONFLICT", "任务状态不允许该操作")



def create_task(
    session: Session,
    settings: Settings,
    *,
    actor_id: str,
    task_type: str,
    scope_key: str,
    idempotency_key: str,
    payload: dict,
    api_version: str,
    request_id: str | None,
) -> AsyncTask:
    """幂等创建：唯一键 (type,scope_key,idempotency_key) 在事务中裁决；
    同键同载荷返回原任务，同键异载荷 409。"""
    payload_hash = canonical_payload_hash(api_version, scope_key, payload)
    existing = session.execute(
        select(AsyncTask).where(
            AsyncTask.type == task_type,
            AsyncTask.scope_key == scope_key,
            AsyncTask.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise TaskRejection(409, "TASK_IDEMPOTENCY_CONFLICT", "幂等键对应不同请求内容")
        return existing

    task_id = str(uuid.uuid4())
    task = AsyncTask(
        id=task_id,
        actor_id=actor_id,
        type=task_type,
        scope_key=scope_key,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        payload=payload,
        status="queued",
        stage="queued",
        progress=0,
        processed=0,
        retry_count=0,
        root_task_id=task_id,
        queued_deadline=now_utc() + timedelta(seconds=settings.task_queue_timeout_seconds),
        request_id=request_id,
    )
    session.add(task)
    session.add(
        TaskOutbox(
            task_id=task_id,
            event_type="execute",
            status="pending",
            attempts=0,
            next_attempt_at=now_utc(),
        )
    )
    add_audit_log(session, actor_id=actor_id, action="task.create", task=task,
                  result="succeeded", request_id=request_id)
    try:
        session.commit()
    except IntegrityError as exc:
        # 并发同键创建：唯一键兜底，失败方按已存在任务裁决。
        session.rollback()
        if "uq_task_idempotency" not in str(exc):
            raise
        existing = session.execute(
            select(AsyncTask).where(
                AsyncTask.type == task_type,
                AsyncTask.scope_key == scope_key,
                AsyncTask.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        if existing.payload_hash != payload_hash:
            raise TaskRejection(
                409, "TASK_IDEMPOTENCY_CONFLICT", "幂等键对应不同请求内容"
            ) from None
        return existing
    return task


def cancel_task(session: Session, task: AsyncTask, *, actor_id: str,
                request_id: str | None) -> tuple[int, str]:
    """协作取消：queued 原子转 cancelled；running 转 cancel_requested；
    终态 409；重复请求幂等。行锁与完成写回串行化。"""
    status = session.execute(
        select(AsyncTask.status).where(AsyncTask.id == task.id).with_for_update()
    ).scalar_one()
    if status == "queued":
        session.execute(
            AsyncTask.__table__.update()
            .where(AsyncTask.id == task.id, AsyncTask.status == "queued")
            .values(status="cancelled", stage="cancelled", finished_at=now_utc(),
                    version=AsyncTask.version + 1)
        )
        add_audit_log(session, actor_id=actor_id, action="task.cancel", task=task,
                      result="succeeded", request_id=request_id)
        session.commit()
        return 204, "cancelled"
    if status == "cancelled":
        return 204, "cancelled"
    if status == "running":
        session.execute(
            AsyncTask.__table__.update()
            .where(AsyncTask.id == task.id, AsyncTask.status == "running")
            .values(status="cancel_requested", version=AsyncTask.version + 1)
        )
        add_audit_log(session, actor_id=actor_id, action="task.cancel", task=task,
                      result="succeeded", request_id=request_id)
        session.commit()
        return 202, "cancel_requested"
    if status == "cancel_requested":
        return 202, "cancel_requested"
    raise TaskRejection(*STATE_CONFLICT)


def add_successor(
    session: Session,
    settings: Settings,
    original: AsyncTask,
    *,
    actor_id: str,
    idempotency_key: str,
    request_id: str | None,
    action: str,
) -> AsyncTask:
    """向会话加入 successor（retry_of 唯一，与 outbox/审计同事务，不提交）。
    调用方须已行锁原任务。"""
    task_id = str(uuid.uuid4())
    successor = AsyncTask(
        id=task_id,
        actor_id=original.actor_id,
        type=original.type,
        scope_key=original.scope_key,
        idempotency_key=idempotency_key,
        payload_hash=original.payload_hash,
        payload=original.payload,
        status="queued",
        stage="queued",
        progress=0,
        processed=0,
        retry_of=original.id,
        root_task_id=original.root_task_id,
        retry_count=original.retry_count + 1,
        queued_deadline=now_utc() + timedelta(seconds=settings.task_queue_timeout_seconds),
        request_id=request_id,
    )
    session.add(successor)
    session.add(
        TaskOutbox(
            task_id=task_id,
            event_type="execute",
            status="pending",
            attempts=0,
            next_attempt_at=now_utc(),
        )
    )
    add_audit_log(session, actor_id=actor_id, action=action, task=successor,
           result="succeeded", request_id=request_id)
    return successor


def retry_task(session: Session, settings: Settings, task: AsyncTask, *,
               actor_id: str, idempotency_key: str,
               request_id: str | None) -> AsyncTask:
    """手动重试：仅 failed/cancelled；同幂等键重放返回原 successor；
    已有 successor 且键不同 409 TASK_RETRY_EXISTS。"""
    status = session.execute(
        select(AsyncTask.status).where(AsyncTask.id == task.id).with_for_update()
    ).scalar_one()
    if status not in ("failed", "cancelled"):
        raise TaskRejection(*STATE_CONFLICT)
    # 锁定读而非快照读：并发重试时第二个请求能看到已提交的 successor。
    existing = session.execute(
        select(AsyncTask).where(AsyncTask.retry_of == task.id).with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if existing.idempotency_key != idempotency_key:
            raise TaskRejection(409, "TASK_RETRY_EXISTS", "该任务已有重试任务")
        return existing
    successor = add_successor(
        session, settings, task, actor_id=actor_id, idempotency_key=idempotency_key,
        request_id=request_id, action="task.retry",
    )
    try:
        session.commit()
    except IntegrityError as exc:
        # 唯一索引兜底：任何交错下原任务最多一个直接 successor。
        session.rollback()
        if "uq_async_task_retry_of" not in str(exc):
            raise
        raise TaskRejection(409, "TASK_RETRY_EXISTS", "该任务已有重试任务") from None
    return successor


def list_tasks(session: Session, *, task_type: str | None, status: str | None,
               page: int, page_size: int) -> tuple[list[AsyncTask], int]:
    query = select(AsyncTask)
    count_query = select(func.count()).select_from(AsyncTask)
    if task_type is not None:
        query = query.where(AsyncTask.type == task_type)
        count_query = count_query.where(AsyncTask.type == task_type)
    if status is not None:
        query = query.where(AsyncTask.status == status)
        count_query = count_query.where(AsyncTask.status == status)
    total = session.execute(count_query).scalar_one()
    rows = session.execute(
        query.order_by(AsyncTask.created_at.desc(), AsyncTask.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    return list(rows), total


def get_task_or_404(session: Session, task_id: str) -> AsyncTask:
    task = fetch_task_or_none(session, task_id)
    if task is None:
        raise TaskRejection(*NOT_FOUND)
    return task


def assert_can_operate(task: AsyncTask, user: User) -> None:
    # Member 仅能取消/重试自己发起的任务，Admin 可操作全部。
    if user.role != "Admin" and task.actor_id != user.id:
        raise TaskRejection(403, "AUTH_FORBIDDEN", "权限不足")


def serialize_task_list(rows: list[AsyncTask]) -> list[dict]:
    return [serialize_task(row) for row in rows]
