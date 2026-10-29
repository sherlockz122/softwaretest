from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response

from apps.api.auth import current_user
from packages.auth.security import require_role
from packages.semantic import asset_catalog
from packages.semantic.asset_reader import ConsumptionError
from packages.semantic.verification import VerificationService

router = APIRouter(prefix="/api/v1/semantic-assets", tags=["semantic-assets"])


async def verification_actor(request: Request, user=Depends(current_user)):
    require_role(user, "Member")
    async for chunk in request.stream():
        if chunk:
            raise ConsumptionError("SEMANTIC_TASK_INVALID_INPUT", 422)
    return user


@router.post("/{asset_id}/verifications", status_code=202)
def verification(
    asset_id: Annotated[str, Path(pattern="^[0-9a-f]{64}$")],
    request: Request,
    response: Response,
    user=Depends(verification_actor),
    key: str = Header(alias="Idempotency-Key"),
):
    response.headers["Cache-Control"] = "no-store"
    return VerificationService(request.app.state.settings, request.app.state.connections).create(
        user, key, asset_id, request.state.request_id
    )


@router.get("")
def listing(
    response: Response,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    response.headers["Cache-Control"] = "no-store"
    return asset_catalog.listing(page, page_size)


@router.get("/{asset_id}")
def detail(
    asset_id: Annotated[str, Path(pattern="^[0-9a-f]{64}$")],
    response: Response,
    user=Depends(current_user),
    offset: int = Query(default=0, ge=0, le=660),
    limit: int = Query(default=100, ge=1, le=100),
    partition: Literal["train", "validation"] | None = None,
):
    response.headers["Cache-Control"] = "no-store"
    return asset_catalog.detail(asset_id, offset, limit, partition)
