import json

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import current_user
from packages.auth.security import require_role
from packages.mining import dataset_import, research_dataset

router = APIRouter(prefix="/api/v1/assets", tags=["assets"])


class ImportPathBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    columns: dict
    license: str = Field(min_length=1, max_length=40)


def _columns(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise dataset_import.ImportError(
            "DATASET_IMPORT_COLUMNS_INVALID", "columns 必须是 JSON 对象"
        ) from exc
    return parsed


async def _read_body(request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > dataset_import.IMPORT_LIMIT_BYTES:
            raise dataset_import.ImportError(
                "DATASET_IMPORT_LIMIT", f"文件超过导入上限（{dataset_import.IMPORT_LIMIT_BYTES // 1024**2} MB）"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/import-columns")
def import_columns(
    request: Request,
    user=Depends(current_user),
    path: str = Query(min_length=1, max_length=512),
):
    require_role(user, "Admin")
    return {"columns": dataset_import.detect_columns(path)}


@router.post("/import", status_code=201)
async def import_upload(
    request: Request,
    user=Depends(current_user),
    columns: str = Query(min_length=2, max_length=4096),
    license: str = Query(min_length=1, max_length=40),
):
    require_role(user, "Member")
    payload = await _read_body(request)
    loaded = dataset_import.import_payload(
        research_dataset.STORE, payload, columns=_columns(columns), license=license
    )
    return research_dataset.preview(loaded["manifest"]["asset_id"])


@router.post("/import-from-path", status_code=201)
def import_from_path(
    body: ImportPathBody,
    request: Request,
    user=Depends(current_user),
):
    require_role(user, "Admin")
    loaded = dataset_import.import_path(
        research_dataset.STORE, body.path, columns=body.columns, license=body.license
    )
    return research_dataset.preview(loaded["manifest"]["asset_id"])
