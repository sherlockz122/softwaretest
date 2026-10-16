from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.api.auth import current_user
from packages.mining.dataset_service import DatasetService

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


class DatasetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    asset_id: str = Field(pattern="^[0-9a-f]{64}$")
    source_hash: str = Field(pattern="^[0-9a-f]{64}$")
    purpose: str = Field(default="published_binary_12_v1", max_length=40)
    split_strategy: Literal["time_ordered", "repository_holdout"] = "time_ordered"
    train_ratio: float = Field(default=0.70, strict=True, gt=0, lt=1)
    validation_ratio: float = Field(default=0.15, strict=True, gt=0, lt=1)
    test_ratio: float = Field(default=0.15, strict=True, gt=0, lt=1)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("invalid display name")
        return value.strip()


def service(request):
    return DatasetService(request.app.state.settings, request.app.state.connections)


@router.post("", status_code=202)
def create(
    body: DatasetBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return service(request).create(user, key, body.model_dump(), request.state.request_id)


@router.get("")
def listing(
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return service(request).listing(page, page_size)


@router.get("/{dataset_id:uuid}")
def detail(
    dataset_id: UUID,
    request: Request,
    user=Depends(current_user),
    offset: int = Query(default=0, ge=0, le=200000),
    limit: int = Query(default=100, ge=1, le=100),
    format: Literal["json", "csv"] = "json",
    partition: Literal["train", "validation", "test"] = "train",
):
    if format == "csv":
        path = service(request).download(str(dataset_id), partition)
        return FileResponse(
            path,
            media_type="text/csv",
            filename=f"{dataset_id}-{partition}.csv",
            headers={"Cache-Control": "no-store"},
        )
    return service(request).detail(str(dataset_id), offset, limit)
