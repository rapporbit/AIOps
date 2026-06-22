"""知识库数据源管理接口.

POST /api/v1/kb/sources              注册飞书数据源 (需 X-KB-Admin-Token)
GET  /api/v1/kb/sources              列出数据源
POST /api/v1/kb/sources/{id}/preview 预览源下文档 (调 list_changes, 不落库; 接入自检用)
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.v1.documents import require_kb_admin_token
from app.schemas.common import ApiResponse
import app.services.kb_source_service as kb_source_service

router = APIRouter(prefix="/kb", tags=["kb-sources"])


class CreateFeishuSourceRequest(BaseModel):
    space_id: str = Field(..., description="飞书 Wiki 知识库 space_id")
    name: str = Field(default="", description="数据源显示名")
    id: Optional[str] = Field(default=None, description="自定义源 id (默认 feishu:wiki:<space_id>)")


@router.post(
    "/sources",
    response_model=ApiResponse,
    summary="注册飞书数据源",
    dependencies=[Depends(require_kb_admin_token)],
)
async def create_source(req: CreateFeishuSourceRequest) -> ApiResponse:
    source_id = req.id or f"feishu:wiki:{req.space_id}"
    source = await kb_source_service.create_source(
        id=source_id,
        type="feishu",
        name=req.name or source_id,
        config={"space_id": req.space_id},
    )
    return ApiResponse.success(
        data={"id": source.id, "type": source.type, "name": source.name, "config": source.config},
        message="数据源已注册",
    )


@router.get("/sources", response_model=ApiResponse, summary="列出数据源")
async def list_sources() -> ApiResponse:
    sources = await kb_source_service.list_sources()
    return ApiResponse.success(
        data=[
            {"id": s.id, "type": s.type, "name": s.name, "config": s.config}
            for s in sources
        ]
    )


@router.post(
    "/sources/{source_id}/preview",
    response_model=ApiResponse,
    summary="预览源下文档 (不落库)",
    description="调用 connector.list_changes 列出该源下受支持的文档, 用于验证鉴权/协作者授权/类型白名单。",
    dependencies=[Depends(require_kb_admin_token)],
)
async def preview_source(source_id: str) -> ApiResponse:
    source = await kb_source_service.get_source(source_id)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="数据源不存在")
    refs = await kb_source_service.preview_source(source)
    return ApiResponse.success(
        data={
            "total": len(refs),
            "documents": [
                {
                    "external_id": r.external_id,
                    "type": r.external_type,
                    "title": r.title,
                    "version": r.version,
                    "need_ocr": r.need_ocr,
                }
                for r in refs
            ],
        },
        message=f"列出 {len(refs)} 篇文档",
    )
