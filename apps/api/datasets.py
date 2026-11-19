from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.api.auth import current_user
from packages.mining.dataset_service import DatasetService

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


class DatasetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    # 默认仍是公开数据资产，仓库分析结果作为显式来源分支。
    source_kind: Literal["asset", "repository"] = "asset"
    asset_id: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    source_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    repository_id: UUID | None = None
    feature_run_id: UUID | None = None
    szz_run_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=32)
    purpose: str = Field(default="published_binary_12_v1", max_length=40)
    split_strategy: Literal["time_ordered", "repository_holdout"] = "time_ordered"
    train_ratio: float = Field(default=0.70, strict=True, gt=0, lt=1)
    # 验证集可选：0 表示不切验证分区
    validation_ratio: float = Field(default=0.15, strict=True, ge=0, lt=1)
    test_ratio: float = Field(default=0.15, strict=True, gt=0, lt=1)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("invalid display name")
        return value.strip()

    @model_validator(mode="after")
    def source_fields(self):
        if self.source_kind == "asset":
            if not (self.asset_id and self.source_hash):
                raise ValueError("asset source needs asset_id and source_hash")
            if self.repository_id or self.feature_run_id or self.szz_run_ids:
                raise ValueError("asset source cannot carry repository rounds")
        else:
            if self.asset_id or self.source_hash:
                raise ValueError("repository source cannot carry asset fields")
            if not (self.repository_id and self.feature_run_id and self.szz_run_ids):
                raise ValueError("repository source needs repository and rounds")
        return self


def service(request):
    return DatasetService(request.app.state.settings, request.app.state.connections)


@router.post("", status_code=202)
def create(
    body: DatasetBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    payload = body.model_dump()
    for field in ("repository_id", "feature_run_id"):
        if payload.get(field) is not None:
            payload[field] = str(payload[field])
    if payload.get("szz_run_ids"):
        payload["szz_run_ids"] = [str(value) for value in payload["szz_run_ids"]]
    return service(request).create(user, key, payload, request.state.request_id)


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
    filter_partition: Literal["train", "validation", "test"] | None = Query(default=None),
    filter_label: int | None = Query(default=None, ge=0, le=1),
):
    if format == "csv":
        path = service(request).download(str(dataset_id), partition)
        return FileResponse(
            path,
            media_type="text/csv",
            filename=f"{dataset_id}-{partition}.csv",
            headers={"Cache-Control": "no-store"},
        )
    return service(request).detail(
        str(dataset_id), offset, limit, filter_partition, filter_label
    )
