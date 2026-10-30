from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from apps.api.auth import current_user
from packages.mining.feature_service import FeatureService
from packages.mining.fix import FixService
from packages.mining.fix_rules import RULE_VERSION
from packages.mining.szz import SZZService
from packages.repositories.parse_policy import COMMIT_LIMIT
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
    # 提交窗口必须二选一显式声明：要么给出 1 到上限之间的条数，要么勾全历史。
    # 留空不再等于全历史，避免一次疏忽就触发要跑几小时的全量解析。
    commit_limit: int | None = Field(default=None, ge=1, le=COMMIT_LIMIT, strict=True)
    full_history: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def one_scope(self):
        if self.full_history == (self.commit_limit is not None):
            raise ValueError("PARSING_SCOPE_INVALID")
        return self


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


class FeatureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature_version: Literal["kamei14-dg-v1", "kamei14-dg-message-v2"] = "kamei14-dg-v1"
    purpose: Literal["historical", "candidate"] = "historical"
    fix_run_id: UUID | None = None

    @model_validator(mode="after")
    def independent_message_version(self):
        if self.feature_version == "kamei14-dg-message-v2" and self.fix_run_id is not None:
            raise ValueError("Message-purpose features cannot consume later Fix review runs")
        return self


def features(request):
    return FeatureService(request.app.state.settings, request.app.state.connections)


@router.post("/{repository_id:uuid}/feature-runs", status_code=202)
def start_features(
    repository_id: UUID,
    body: FeatureBody,
    request: Request,
    user=Depends(current_user),
    key: str = Header(alias="Idempotency-Key"),
):
    policy = body.model_dump() | {"fix_run_id": str(body.fix_run_id) if body.fix_run_id else None}
    return features(request).create(user, str(repository_id), key, policy, request.state.request_id)


@router.get("/{repository_id:uuid}/feature-runs")
def feature_runs(
    repository_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return features(request).runs(str(repository_id), page, page_size)


@router.get("/{repository_id:uuid}/feature-quality")
def feature_quality(
    repository_id: UUID,
    run_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    feature: Literal[
        "NS",
        "ND",
        "NF",
        "Entropy",
        "LA",
        "LD",
        "LT",
        "FIX",
        "NDEV",
        "AGE",
        "NUC",
        "EXP",
        "REXP",
        "SEXP",
    ]
    | None = None,
    missing: bool | None = None,
):
    return features(request).results(
        str(repository_id), str(run_id), page, page_size, feature, missing
    )


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
    item_id: UUID | None = Query(default=None),
):
    return szzing(request).links(
        str(run_id), page, page_size, str(item_id) if item_id else None
    )


@szz_router.get("/{run_id:uuid}/results")
def szz_results(
    run_id: UUID,
    request: Request,
    user=Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    attention: bool = Query(default=False),
):
    return szzing(request).results(str(run_id), page, page_size, attention)


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
        user,
        str(repository_id),
        key,
        None if body.full_history else body.commit_limit,
        request.state.request_id,
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
    attention: bool = Query(default=False),
):
    return fixing(request).results(
        str(repository_id), str(run_id), page, page_size, attention
    )


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
