"""A29～A32/A35 任务接口（docs/04 第 10.2/10.3 节）。"""

import json

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from apps.api.security import get_db_session, require_auth, require_role
from packages.platform.config import Settings
from packages.tasks.domain import (
    TASK_STATUSES,
    TaskRejection,
    serialize_task,
    validate_idempotency_key,
)
from packages.tasks.service import (
    assert_can_operate,
    cancel_task,
    create_task,
    get_task_or_404,
    list_tasks,
    retry_task,
)

TASK_TYPE_DIAGNOSTICS = "diagnostics"
API_VERSION = "v1"

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
diagnostics_router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

INVALID = (422, "TASK_INVALID_INPUT", "请求参数无效")


def _require_idempotency_key(request: Request) -> str:
    # 缺失/空/超长/非可见 ASCII 统一 422 TASK_INVALID_INPUT。
    key = request.headers.get("Idempotency-Key", "")
    validate_idempotency_key(key)
    return key


def _diagnostics_not_in_production(request: Request) -> None:
    # production 不暴露诊断入口：与未挂载路由同样返回 404（且先于认证检查）。
    if request.app.state.settings.environment == "production":
        raise TaskRejection(404, "SYSTEM_NOT_FOUND", "请求的资源或操作不可用")


@diagnostics_router.post(
    "/diagnostics", status_code=202, dependencies=[Depends(_diagnostics_not_in_production)]
)
def create_diagnostics(
    request: Request,
    body: dict | None = Body(default=None),
    auth=Depends(require_role("Member", "Admin")),
    session=Depends(get_db_session),
):
    """A35：仅 development/test 挂载；只接受 {"duration_seconds":0～30 整数}。"""
    settings: Settings = request.app.state.settings
    if not isinstance(body, dict) or set(body) - {"duration_seconds"}:
        raise TaskRejection(*INVALID)
    duration = body.get("duration_seconds", 1)
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= 30:
        raise TaskRejection(*INVALID)
    key = _require_idempotency_key(request)
    payload = {"duration_seconds": duration}
    if len(json.dumps(payload).encode("utf-8")) > settings.task_max_payload_bytes:
        raise TaskRejection(*INVALID)
    task = create_task(
        session, settings, actor_id=auth.user.id, task_type=TASK_TYPE_DIAGNOSTICS,
        scope_key=f"user:{auth.user.id}:diagnostics", idempotency_key=key,
        payload=payload, api_version=API_VERSION, request_id=request.state.request_id,
    )
    return {"task_id": task.id, "status": task.status}


@router.get("")
def list_tasks_endpoint(
    task_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, alias="page"),
    page_size: int = Query(default=20, alias="page_size"),
    auth=Depends(require_auth),
    session=Depends(get_db_session),
):
    """A29：共享任务列表，默认 created_at desc,id asc；非法过滤 422。"""
    if task_type is not None and (
        not task_type or len(task_type) > 64
        or any(ord(c) < 0x21 or ord(c) > 0x7E for c in task_type)
    ):
        raise TaskRejection(*INVALID)
    if status is not None and status not in TASK_STATUSES:
        raise TaskRejection(*INVALID)
    if not 1 <= page <= 1_000_000:
        raise TaskRejection(*INVALID)
    if not 1 <= page_size <= 100:
        raise TaskRejection(*INVALID)
    rows, total = list_tasks(
        session, task_type=task_type, status=status, page=page, page_size=page_size
    )
    return {
        "items": [serialize_task(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{task_id}")
def get_task(
    task_id: str,
    auth=Depends(require_auth),
    session=Depends(get_db_session),
):
    """A30：任务详情；不存在 404 TASK_NOT_FOUND，内部 token/hash 不入响应。"""
    return serialize_task(get_task_or_404(session, task_id))


@router.post("/{task_id}/retry", status_code=202)
def retry(
    request: Request,
    task_id: str,
    auth=Depends(require_role("Member", "Admin")),
    session=Depends(get_db_session),
):
    """A31：仅 failed/cancelled；同幂等键重放返回原 successor。"""
    settings: Settings = request.app.state.settings
    key = _require_idempotency_key(request)
    task = get_task_or_404(session, task_id)
    assert_can_operate(task, auth.user)
    successor = retry_task(
        session, settings, task, actor_id=auth.user.id, idempotency_key=key,
        request_id=request.state.request_id,
    )
    return {
        "task_id": successor.id,
        "retry_of": successor.retry_of,
        "status": successor.status,
    }


@router.post("/{task_id}/cancel")
def cancel(
    request: Request,
    task_id: str,
    auth=Depends(require_role("Member", "Admin")),
    session=Depends(get_db_session),
):
    """A32：queued→cancelled 204；running→cancel_requested 202；终态 409。"""
    task = get_task_or_404(session, task_id)
    assert_can_operate(task, auth.user)
    status_code, status = cancel_task(
        session, task, actor_id=auth.user.id, request_id=request.state.request_id
    )
    if status_code == 204:
        return Response(status_code=204)
    return JSONResponse(status_code=202, content={"task_id": task_id, "status": status})
