from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from apps.api.auth import current_user
from packages.prediction.service import PredictionService

router = APIRouter(prefix="/api/v1", tags=["predictions"])

DIFF_MAX = 2 * 1024 * 1024


class CandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    model_run_id: UUID
    base_ref: str = Field(min_length=1, max_length=255)
    candidate_ref: str | None = Field(default=None, max_length=255)
    candidate_diff: str | None = Field(default=None, max_length=DIFF_MAX)

    @model_validator(mode="after")
    def exactly_one_candidate(self):
        if (self.candidate_ref is None) == (self.candidate_diff is None):
            raise ValueError("candidate_ref 与 candidate_diff 必须且只能提供其一")
        return self


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    model_run_id: UUID
    base_ref: str = Field(min_length=1, max_length=255)
    window_days: StrictInt = Field(default=90, ge=1, le=90)
    max_candidates: StrictInt = Field(default=500, ge=1, le=500)


class LlmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    sha: str = Field(min_length=7, max_length=64)
    provider: str = Field(default="deepseek", max_length=32)


def service(request):
    return PredictionService(request.app.state.settings, request.app.state.connections)


@router.post("/predictions/candidate", status_code=202)
def candidate(
    body: CandidateBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return service(request).create_single(
        user, key, body.model_dump(mode="json"), request.state.request_id
    )


@router.post("/prediction-tasks", status_code=202)
def batch(
    body: BatchBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return service(request).create_batch(
        user, key, body.model_dump(mode="json"), request.state.request_id
    )


@router.get("/prediction-tasks/{task_id:uuid}/results")
def results(
    task_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    level: Literal["high", "medium", "low"] | None = None,
    oversized: bool | None = None,
):
    return service(request).queue(str(task_id), page, page_size, level, oversized)


@router.get("/predictions/{prediction_id:uuid}/explanation")
def explanation(prediction_id: UUID, request: Request, user=Depends(current_user)):
    return service(request).explanation(str(prediction_id))


@router.get("/predictions/llm/providers")
def llm_providers(request: Request, user=Depends(current_user)):
    """可选的大模型入口，未配置的也会返回，标记为 configured=false。"""
    from packages.prediction import llm as llm_review

    return {"items": llm_review.providers(request.app.state.settings)}


@router.post("/predictions/llm")
def llm(body: LlmBody, request: Request, user=Depends(current_user)):
    """大模型评审：只看提交说明和改动概况，不发送源码。"""
    from packages.prediction import llm as llm_review

    return llm_review.review(
        request.app.state.settings,
        request.app.state.connections.engine,
        str(body.repository_id),
        body.sha,
        body.provider,
    )
