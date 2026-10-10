from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from apps.api.auth import current_user
from packages.mining.fix import FixService
from packages.mining.fix_rules import RULE_VERSION
from packages.mining.szz import SZZService
from packages.repositories.parsing import ParsingService
from packages.repositories.service import RepositoryService
from packages.repositories.sync import SyncService

router = APIRouter(prefix="/api/v1/repositories", tags=["repositories"])
szz_router = APIRouter(prefix="/api/v1/szz-runs", tags=["szz"])


class RepositoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=1024)


class ParseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_limit: int | None = Field(default=None, ge=1, le=10000000, strict=True)


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FixBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_version: Literal["fix-evidence-v1"] = RULE_VERSION
    include_medium: bool = Field(default=True, strict=True)


class FixReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "rejected", "unreviewed"]
    expected_revision: int = Field(ge=0, strict=True)
    note: str = Field(min_length=1, max_length=300, pattern=r"\S")


class SZZBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fix_run_id: UUID
    algorithm_version: Literal["baseline-szz-v1"] = "baseline-szz-v1"
    as_of: AwareDatetime | None = None


def szzing(request):
    return SZZService(request.app.state.settings, request.app.state.connections)


@router.post("/{repository_id:uuid}/szz-runs", status_code=202)
def start_szz(
    repository_id: UUID,
    body: SZZBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    policy = {
        "fix_run_id": str(body.fix_run_id),
        "algorithm_version": body.algorithm_version,
        "as_of": body.as_of.isoformat() if body.as_of else None,
    }
    return szzing(request).create(user, str(repository_id), key, policy, request.state.request_id)


@router.get("/{repository_id:uuid}/szz-runs")
def szz_runs(
    repository_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return szzing(request).runs(str(repository_id), page, page_size)


@szz_router.get("/{run_id:uuid}/links")
def szz_links(
    run_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return szzing(request).links(str(run_id), page, page_size)


@szz_router.get("/{run_id:uuid}/results")
def szz_results(
    run_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return szzing(request).results(str(run_id), page, page_size)


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


@router.post("/{repository_id:uuid}/sync", status_code=202)
def sync(
    repository_id: UUID,
    body: SyncBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return SyncService(request.app.state.settings, request.app.state.connections).create(
        user, str(repository_id), key, request.state.request_id
    )


@router.get("/{repository_id:uuid}/sync-windows")
def sync_windows(
    repository_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return SyncService(request.app.state.settings, request.app.state.connections).windows(
        str(repository_id), page, page_size
    )


def fixing(request):
    return FixService(request.app.state.settings, request.app.state.connections)


@router.post("/{repository_id:uuid}/fix-detection", status_code=202)
def detect_fix(
    repository_id: UUID,
    body: FixBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    return fixing(request).create(
        user, str(repository_id), key, body.model_dump(), request.state.request_id
    )


@router.get("/{repository_id:uuid}/fix-runs")
def fix_runs(
    repository_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return fixing(request).runs(str(repository_id), page, page_size)


@router.get("/{repository_id:uuid}/fix-runs/{run_id:uuid}/evidence")
def fix_evidence(
    repository_id: UUID,
    run_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return fixing(request).results(str(repository_id), str(run_id), page, page_size)


@router.patch("/{repository_id:uuid}/fix-evidence/{assessment_id:uuid}/review")
def review_fix(
    repository_id: UUID,
    assessment_id: UUID,
    body: FixReviewBody,
    request: Request,
    user=Depends(current_user),
):
    return fixing(request).review(
        user, str(repository_id), str(assessment_id), body.model_dump(), request.state.request_id
    )
