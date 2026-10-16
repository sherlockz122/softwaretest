from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import current_user
from packages.modeling.service import ModelService

router = APIRouter(prefix="/api/v1", tags=["models"])


class ModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: UUID
    plugin: Literal["lr", "rf"]
    params: dict = Field(default_factory=dict)
    seed: int = Field(default=42, strict=True, ge=0, le=2**31 - 1)


def service(request):
    return ModelService(request.app.state.settings, request.app.state.connections)


@router.post("/model-runs", status_code=202)
def create(
    body: ModelBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    payload = body.model_dump() | {"dataset_id": str(body.dataset_id)}
    return service(request).create(user, key, payload, request.state.request_id)


@router.get("/model-runs/{run_id:uuid}")
def detail(
    run_id: UUID,
    request: Request,
    user=Depends(current_user),
    artifact: Literal["model.skops", "report.json", "probabilities.csv"] | None = None,
):
    if artifact:
        path = service(request).download(str(run_id), artifact)
        return FileResponse(
            path, filename=f"{run_id}-{artifact}", headers={"Cache-Control": "no-store"}
        )
    return service(request).detail(str(run_id))


@router.get("/models")
def listing(
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return service(request).listing(page, page_size)


@router.post("/model-runs/{run_id:uuid}/publish")
def publish(run_id: UUID, request: Request, user=Depends(current_user)):
    return service(request).release(user, str(run_id))
