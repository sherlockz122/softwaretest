"""可靠任务的状态常量、规范化哈希与安全化输出（docs/04 第 10.2～10.5 节）。"""

import hashlib
import json
import re

from sqlalchemy.orm import Session

from packages.domain.models import AsyncTask, OperationLog
from packages.security.tokens import as_utc

TASK_STATUSES = ("queued", "running", "cancel_requested", "succeeded", "failed", "cancelled")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
EXECUTE_TASK_NAME = "defectguard.execute"
# 基础设施类失败才允许自动恢复；业务失败与用户取消不自动恢复。
RETRYABLE_INFRA_ERRORS = ("TASK_DISPATCH_FAILED", "TASK_QUEUE_TIMEOUT", "TASK_LEASE_EXPIRED")

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,128}$")
ERROR_MESSAGE_PATTERN = re.compile(r"[^\w\s一-鿿.,:;!?()\-=+*/@#\[\]{}]")

ID_ERROR_CODE = "TASK_INVALID_INPUT"
ID_CONFLICT_CODE = "TASK_IDEMPOTENCY_CONFLICT"


class TaskRejection(Exception):
    """由统一异常处理器转为错误响应的任务拒绝。"""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def validate_idempotency_key(value: str) -> None:
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise TaskRejection(422, ID_ERROR_CODE, "请求参数无效")


def canonical_payload_hash(api_version: str, scope_key: str, payload: dict) -> str:
    # 接口版本、规范化类型化输入（键排序、无 NaN）与作用域参与哈希，不含令牌。
    canonical = json.dumps(
        {"api_version": api_version, "scope_key": scope_key, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def delivery_delay_seconds(attempts: int) -> int:
    # 第 n 次尝试的退避：min(5*2^(n-1), 60) 秒。
    return min(5 * 2 ** (attempts - 1), 60)


def sanitize_error_message(value: str, limit: int = 512) -> str:
    # 不泄露本机路径/SQL/命令/secret；仅保留可展示字符。
    cleaned = ERROR_MESSAGE_PATTERN.sub(" ", value)[:limit].strip()
    return cleaned or "执行失败"


def _iso(value):
    return as_utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def serialize_task(task: AsyncTask) -> dict:
    # 租约内部 token、payload_hash、用户秘密与原始日志不进入响应。
    now = as_utc(task.created_at) if task.created_at else None
    if task.status in ("running", "cancel_requested") and task.lease_until is not None:
        lease = as_utc(task.lease_until)
        health = "stale" if now is not None and lease <= now else "healthy"
    else:
        health = "healthy"
    return {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "stage": task.stage,
        "progress": float(task.progress),
        "processed": task.processed,
        "total": task.total,
        "heartbeat_at": _iso(task.heartbeat_at),
        "created_at": _iso(task.created_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
        "health": health,
        "retry_of": task.retry_of,
        "result": task.result_json,
        "error": (
            {
                "code": task.error_code,
                "message": task.error_message,
                "request_id": task.request_id,
            }
            if task.error_code is not None
            else None
        ),
    }


def task_digest(task: AsyncTask) -> str:
    return f"{task.id} {task.type} {task.status}"


def fetch_task_or_none(session: Session, task_id: str) -> AsyncTask | None:
    return session.get(AsyncTask, task_id)


def add_audit_log(session: Session, *, actor_id: str, action: str, task: AsyncTask,
                  result: str, request_id: str | None) -> None:
    """任务审计：与业务变更同事务加入，不单独提交。"""
    session.add(
        OperationLog(
            actor_id=actor_id,
            action=action,
            object_type="async_task",
            object_id=task.id,
            result=result,
            request_id=request_id,
        )
    )
