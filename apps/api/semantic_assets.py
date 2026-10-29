from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Response

from apps.api.auth import current_user
from packages.semantic import asset_catalog

router = APIRouter(prefix="/api/v1/semantic-assets", tags=["semantic-assets"])


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
