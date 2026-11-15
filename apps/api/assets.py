"""公开数据资产的导入入口。

只接收上传的文本与显式列映射；不提供出网下载，也不接受压缩包。
"""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import current_user, json_check
from packages.auth.security import require_role
from packages.mining import asset_import

router = APIRouter(prefix="/api/v1", tags=["assets"])

LICENSES = Literal["CC-BY-4.0", "Apache-2.0", "MIT", "BSD-3-Clause"]


class AssetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_name: str = Field(min_length=1, max_length=120)
    license: LICENSES
    content: str = Field(min_length=1, max_length=asset_import.MAX_TEXT_BYTES)
    mapping: dict[str, str] = Field(default_factory=dict)


@router.post("/public-assets", status_code=201, dependencies=[Depends(json_check)])
def import_asset(body: AssetBody, request: Request, user=Depends(current_user)):
    require_role(user, "Member")
    summary = asset_import.import_upload(
        body.content,
        body.mapping,
        license=body.license,
        source_name=body.source_name,
        user_id=user.id,
    )
    return {
        "asset_id": summary["asset_id"],
        "source_hash": summary["source_hash"],
        "rows": summary["rows"],
        "labels": summary["labels"],
        "source_name": body.source_name,
        "license": body.license,
    }
