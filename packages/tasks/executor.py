"""Worker 侧执行原语：原子领取、心跳续租、条件写回与取消检查点（docs/04 第 10.5 节）。"""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from packages.domain.models import AsyncTask
from packages.platform.config import Settings
from packages.security.tokens import now_utc
from packages.tasks.domain import add_audit_log


def claim_task(session: Session, settings: Settings, task_id: str, token: str) -> bool:
    """按 status=queued 原子领取；重复消息/已取消任务领取失败即安全结束。"""
    now = now_utc()
    result = session.execute(
        update(AsyncTask)
        .where(AsyncTask.id == task_id, AsyncTask.status == "queued")
        .values(
            status="running",
            stage="running",
            execution_token=token,
            lease_until=now + timedelta(seconds=settings.task_execution_lease_seconds),
            heartbeat_at=now,
            started_at=now,
            version=AsyncTask.version + 1,
        )
    )
    session.commit()
    return result.rowcount == 1


def heartbeat_task(session: Session, settings: Settings, task_id: str, token: str) -> bool:
    """续租：仅当仍 running 且令牌一致时生效；返回是否仍持有执行权。"""
    now = now_utc()
    result = session.execute(
        update(AsyncTask)
        .where(
            AsyncTask.id == task_id,
            AsyncTask.status == "running",
            AsyncTask.execution_token == token,
        )
        .values(
            heartbeat_at=now,
            lease_until=now + timedelta(seconds=settings.task_execution_lease_seconds),
            version=AsyncTask.version + 1,
        )
    )
    session.commit()
    return result.rowcount == 1


def read_status(session: Session, task_id: str) -> str:
    return session.execute(
        select(AsyncTask.status).where(AsyncTask.id == task_id)
    ).scalar_one()


def _terminal_update(session: Session, task_id: str, token: str, now, *, terminal: str,
                     values: dict) -> bool:
    """同一事务内条件写回：检查 status/token/lease 尚有效；返回是否写入。"""
    result = session.execute(
        update(AsyncTask)
        .where(
            AsyncTask.id == task_id,
            AsyncTask.status == "running",
            AsyncTask.execution_token == token,
            AsyncTask.lease_until > now,
        )
        .values(
            status=terminal,
            finished_at=now,
            execution_token=None,
            lease_until=None,
            version=AsyncTask.version + 1,
            **values,
        )
    )
    return result.rowcount == 1


def _cancel_checkpoint(session: Session, task_id: str, token: str, now) -> bool:
    """取消检查点：cancel_requested 先提交时成功提交失败，改为写 cancelled。"""
    result = session.execute(
        update(AsyncTask)
        .where(
            AsyncTask.id == task_id,
            AsyncTask.status == "cancel_requested",
            AsyncTask.execution_token == token,
        )
        .values(
            status="cancelled",
            stage="cancelled",
            finished_at=now,
            execution_token=None,
            lease_until=None,
            version=AsyncTask.version + 1,
        )
    )
    return result.rowcount == 1


def finish_task(session: Session, task_id: str, token: str, *, succeeded: bool,
                result_json: dict | None = None, error_code: str | None = None,
                error_message: str | None = None, actor_id: str) -> str | None:
    """完成写回：成功/失败条件写回，失败时走取消检查点。
    返回最终状态；None 表示失去执行权（协调器接管），不改写任务。"""
    now = now_utc()
    if succeeded:
        if _terminal_update(session, task_id, token, now, terminal="succeeded",
                            values={"stage": "done", "progress": 100,
                                    "result_json": result_json}):
            task = session.get(AsyncTask, task_id)
            add_audit_log(session, actor_id=actor_id, action="task.finish", task=task,
                          result="succeeded", request_id=task.request_id)
            session.commit()
            return "succeeded"
        if _cancel_checkpoint(session, task_id, token, now):
            task = session.get(AsyncTask, task_id)
            add_audit_log(session, actor_id=actor_id, action="task.cancel", task=task,
                          result="succeeded", request_id=task.request_id)
            session.commit()
            return "cancelled"
        session.commit()
        return None
    if _terminal_update(session, task_id, token, now, terminal="failed",
                        values={"stage": "failed", "error_code": error_code,
                                "error_message": error_message}):
        task = session.get(AsyncTask, task_id)
        add_audit_log(session, actor_id=actor_id, action="task.finish", task=task,
                      result="failed", request_id=task.request_id)
        session.commit()
        return "failed"
    session.commit()
    return None
