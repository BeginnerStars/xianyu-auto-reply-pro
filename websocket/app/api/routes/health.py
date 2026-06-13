"""WebSocket 健康检查路由"""
from __future__ import annotations

from fastapi import APIRouter
from common.schemas.common import ApiResponse

router = APIRouter(prefix="/health", tags=["健康检查"])


@router.get("/ping")
async def ping() -> ApiResponse:
    """健康检查接口"""
    return ApiResponse(success=True, message="ok", data={"status": "ok"})
