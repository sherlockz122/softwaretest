from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import current_user
from packages.repositories.parsing import ParsingService
from packages.repositories.service import RepositoryService

router = APIRouter(prefix="/api/v1/repositories", tags=["repositories"])


class RepositoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=1024)


class ParseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_limit: int | None = Field(default=None, ge=1, le=10000000, strict=True)


def service(request):
    return RepositoryService(request.app.state.settings, request.app.state.connections)


@router.post("", status_code=202)
def create(
    body: RepositoryBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return service(request).create(user, key, body.url, request.state.request_id)


@router.get("")
def listing(
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return service(request).listing(page, page_size)


@router.get("/{repository_id:uuid}")
def detail(repository_id: UUID, request: Request, user=Depends(current_user)):
    return service(request).detail(str(repository_id))


def parsing(request):
    return ParsingService(request.app.state.settings, request.app.state.connections)


@router.post("/{repository_id:uuid}/parse", status_code=202)
def parse(
    repository_id: UUID,
    body: ParseBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return parsing(request).create(
        user, str(repository_id), key, body.commit_limit, request.state.request_id
    )


@router.get("/{repository_id:uuid}/commits")
def commits(
    repository_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return parsing(request).listing(str(repository_id), page, page_size)


@router.get("/{repository_id:uuid}/commits/{sha}/files")
def files(
    repository_id: UUID,
    sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")],
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return parsing(request).files(str(repository_id), sha, page, page_size)
