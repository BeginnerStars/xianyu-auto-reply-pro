"""
发货监控路由模块

提供发货统计、告警和对话摘要接口
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.services.ai_conversation_service import AIConversationService
from app.services.dashboard_stats_service import DashboardStatsService
from common.models import User
from common.schemas.common import ApiResponse
from common.utils.auth_scope import resolve_owner_scope

router = APIRouter(prefix="/delivery-monitor", tags=["发货监控"])


@router.get("/stats")
async def get_delivery_stats(
    period: str = Query("today", description="时间范围: today/yesterday/7days/30days"),
    current_user: User = Depends(deps.get_current_active_user),
    db: AsyncSession = Depends(deps.get_db_session),
) -> ApiResponse:
    """
    获取发货统计

    返回指定时间范围内的发货统计数据，包括：
    - 总发货数、成功数、失败数、成功率
    - 平均发货时间
    - 失败原因分布
    - 每小时发货量（用于图表）

    Args:
        period: 时间范围
        current_user: 当前登录用户
        db: 数据库会话

    Returns:
        ApiResponse: 包含发货统计数据
    """
    try:
        owner_id, is_admin = resolve_owner_scope(current_user)

        service = DashboardStatsService(db)
        stats = await service.get_delivery_stats(
            period=period,
            owner_id=None if is_admin else owner_id,
        )

        return ApiResponse(
            success=True,
            message="获取成功",
            data=stats,
        )
    except Exception as e:
        return ApiResponse(
            success=False,
            message=f"获取发货统计失败: {str(e)}",
            data=None,
        )


@router.get("/alerts")
async def get_delivery_alerts(
    current_user: User = Depends(deps.get_current_active_user),
    db: AsyncSession = Depends(deps.get_db_session),
) -> ApiResponse:
    """
    获取发货相关告警

    返回当前活跃的告警列表，包括：
    - 发货失败率过高
    - 卡券库存不足
    - 最近1小时失败激增

    Args:
        current_user: 当前登录用户
        db: 数据库会话

    Returns:
        ApiResponse: 包含告警列表
    """
    try:
        service = DashboardStatsService(db)
        alerts = await service.get_delivery_alerts()

        return ApiResponse(
            success=True,
            message="获取成功",
            data=alerts,
        )
    except Exception as e:
        return ApiResponse(
            success=False,
            message=f"获取告警失败: {str(e)}",
            data=None,
        )


@router.get("/summary/{chat_id}")
async def get_conversation_summary(
    chat_id: str,
    cookie_id: str = Query(..., description="账号ID"),
    max_length: int = Query(200, description="摘要最大字符数"),
    current_user: User = Depends(deps.get_current_active_user),
    db: AsyncSession = Depends(deps.get_db_session),
) -> ApiResponse:
    """
    获取对话摘要

    使用AI模型为指定对话生成简洁摘要，包含：
    - 对话主题
    - 用户主要需求
    - 当前状态（是否已成交/议价中/咨询中）

    摘要会缓存30分钟。

    Args:
        chat_id: 聊天ID
        cookie_id: 账号ID
        max_length: 摘要最大字符数
        current_user: 当前登录用户
        db: 数据库会话

    Returns:
        ApiResponse: 包含对话摘要
    """
    try:
        service = AIConversationService(db)
        summary = await service.generate_summary(
            chat_id=chat_id,
            cookie_id=cookie_id,
            max_length=max_length,
        )

        if summary:
            return ApiResponse(
                success=True,
                message="获取成功",
                data={"summary": summary},
            )
        else:
            return ApiResponse(
                success=False,
                message="无法生成对话摘要（可能没有对话记录或AI服务未配置）",
                data=None,
            )
    except Exception as e:
        return ApiResponse(
            success=False,
            message=f"获取对话摘要失败: {str(e)}",
            data=None,
        )
