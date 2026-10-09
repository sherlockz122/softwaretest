from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from apps.api.auth import current_user
from packages.tasks.service import TaskService

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
diagnostic_router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class DiagnosticBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_seconds: StrictInt = Field(default=1, ge=0, le=30)


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def service(request):
    return TaskService(request.app.state.settings, request.app.state.connections)


@diagnostic_router.post("/diagnostic", status_code=202)
def diagnostic(
    body: DiagnosticBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return service(request).create(user, key, body.model_dump(), request.state.request_id)


@router.get("")
def listing(
    request: Request,
    user=Depends(current_user),
    type: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return service(request).listing(type, status, page, page_size)


@router.get("/{task_id:uuid}")
def detail(task_id: UUID, request: Request, user=Depends(current_user)):
    return service(request).detail(str(task_id))


@router.post("/{task_id:uuid}/cancel")
def cancel(task_id: UUID, request: Request, user=Depends(current_user)):
    status, body = service(request).cancel(user, str(task_id), request.state.request_id)
    return (
        Response(status_code=204)
        if status == 204
        else JSONResponse(status_code=status, content=body)
    )


@router.post("/{task_id:uuid}/retry", status_code=202)
def retry(
    task_id: UUID,
    request: Request,
    body: RetryBody | None = None,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return service(request).retry(user, str(task_id), key, request.state.request_id)
