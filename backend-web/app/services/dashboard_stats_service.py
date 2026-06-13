"""
仪表盘统计服务。

功能：
1. 统一普通用户与管理员仪表盘统计查询。
2. 减少重复 count、Python 层统计和多次扫描同一张表的开销。
3. 在不改变接口返回格式的前提下优化首页统计速度。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.http_client import get_http_client
from app.services.auto_reply_stats_service import AutoReplyStatsService
from app.services.dashboard_stats_cache_service import DashboardStatsCacheService
from common.models.agent_order import AgentOrder
from common.models.card import Card
from common.models.user import User
from common.models.xy_account import XYAccount
from common.models.xy_keyword_rule import XYKeywordRule
from common.models.xy_order import XYOrder
from common.services.account_limit_service import AccountLimitService

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))


class DashboardStatsService:
    """仪表盘统计聚合服务。"""

    # Online cookies cache: (timestamp, count)
    _online_cookies_cache: tuple[float, int] | None = None
    _ONLINE_COOKIES_CACHE_TTL = 10  # seconds

    INACTIVE_ACCOUNT_STATUSES = ("inactive", "disabled", "suspended", "deleted")
    # 已关闭/已退款订单：不计入营收、有效订单与待处理统计
    CLOSED_ORDER_STATUSES = ("cancelled", "已关闭", "refunded", "退款成功", "已退款")
    SHIPPED_ORDER_STATUSES = ("shipped", "completed", "已发货", "已完成")
    PENDING_EXCLUDED_ORDER_STATUSES = (*CLOSED_ORDER_STATUSES, *SHIPPED_ORDER_STATUSES)

    def __init__(self, session: AsyncSession):
        self.session = session

    @classmethod
    def _build_today_start(cls) -> datetime:
        return datetime.now(BEIJING_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=None,
        )

    @classmethod
    def _build_enabled_account_condition(cls):
        return or_(
            XYAccount.status.is_(None),
            XYAccount.status == "",
            XYAccount.status.notin_(cls.INACTIVE_ACCOUNT_STATUSES),
        )

    @classmethod
    def _merge_trend_rows(cls, rows, amount_map: dict[str, float], count_map: dict[str, int]) -> None:
        for row in rows:
            date_str = row.order_date.strftime("%m-%d") if hasattr(row.order_date, "strftime") else str(row.order_date)
            amount_map[date_str] = round(amount_map.get(date_str, 0) + float(row.daily_amount or 0), 2)
            count_map[date_str] = count_map.get(date_str, 0) + int(row.daily_count or 0)

    @classmethod
    def _merge_order_summary_row(cls, summary: dict[str, int | float], row) -> None:
        summary["today_orders"] = int(summary["today_orders"]) + int(row.today_orders or 0)
        summary["today_shipped"] = int(summary["today_shipped"]) + int(row.today_shipped or 0)
        summary["today_pending"] = int(summary["today_pending"]) + int(row.today_pending or 0)
        summary["today_amount"] = float(summary["today_amount"]) + float(row.today_amount or 0)

    async def _get_today_order_summary_row(self, *, time_column, start_time: datetime):
        stmt = (
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (XYOrder.status.notin_(self.CLOSED_ORDER_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("today_orders"),
                func.coalesce(
                    func.sum(
                        case(
                            (XYOrder.status.in_(self.SHIPPED_ORDER_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("today_shipped"),
                func.coalesce(
                    func.sum(
                        case(
                            (XYOrder.status.notin_(self.PENDING_EXCLUDED_ORDER_STATUSES), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("today_pending"),
                func.coalesce(
                    func.sum(
                        case(
                            (XYOrder.status.notin_(self.CLOSED_ORDER_STATUSES), XYOrder.amount),
                            else_=0,
                        )
                    ),
                    0,
                ).label("today_amount"),
            )
            .select_from(XYOrder)
            .where(time_column >= start_time)
        )
        return (await self.session.execute(stmt)).one()

    async def _get_limit_status(self, owner_id: int) -> dict[str, int | None]:
        return await DashboardStatsCacheService.get_user_limit_status(
            owner_id,
            lambda: AccountLimitService(self.session).get_status(owner_id),
        )

    async def _load_admin_dashboard_bundle(self) -> dict[str, dict[str, int | float]]:
        today_start = self._build_today_start()

        users_stmt = select(func.count()).select_from(User)
        total_users = int((await self.session.execute(users_stmt)).scalar() or 0)

        accounts_stmt = select(
            func.count().label("total_accounts"),
            func.coalesce(
                func.sum(
                    case(
                        (self._build_enabled_account_condition(), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("active_accounts"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                XYAccount.login_password.isnot(None),
                                XYAccount.login_password != "",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("password_configured"),
        ).select_from(XYAccount)
        accounts_row = (await self.session.execute(accounts_stmt)).one()

        keywords_stmt = select(func.count()).select_from(XYKeywordRule)
        total_keywords = int((await self.session.execute(keywords_stmt)).scalar() or 0)

        orders_stmt = select(
            func.coalesce(
                func.sum(
                    case(
                        (XYOrder.status.notin_(self.CLOSED_ORDER_STATUSES), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("total_orders"),
        ).select_from(XYOrder)
        orders_row = (await self.session.execute(orders_stmt)).one()

        cards_stmt = select(func.count()).select_from(Card)
        total_cards = int((await self.session.execute(cards_stmt)).scalar() or 0)

        reply_stats = await AutoReplyStatsService(self.session).get_today_and_yesterday_success_reply_counts()

        today_users_stmt = select(func.count()).select_from(User).where(User.created_at >= today_start)
        today_users = int((await self.session.execute(today_users_stmt)).scalar() or 0)

        today_accounts_stmt = select(func.count()).select_from(XYAccount).where(XYAccount.created_at >= today_start)
        today_accounts = int((await self.session.execute(today_accounts_stmt)).scalar() or 0)

        today_order_summary: dict[str, int | float] = {
            "today_orders": 0,
            "today_shipped": 0,
            "today_pending": 0,
            "today_amount": 0.0,
        }
        # 只按真实下单时间(placed_at)统计，不对 created_at 做回退，
        # 避免同步历史订单时 created_at=今天被误算为今日订单
        placed_row = await self._get_today_order_summary_row(time_column=XYOrder.placed_at, start_time=today_start)
        self._merge_order_summary_row(today_order_summary, placed_row)

        today_agent_orders_stmt = select(func.count()).select_from(AgentOrder).where(AgentOrder.created_at >= today_start)
        today_agent_orders = int((await self.session.execute(today_agent_orders_stmt)).scalar() or 0)

        return {
            "admin_stats": {
                "total_users": total_users,
                "total_cookies": int(accounts_row.total_accounts or 0),
                "active_cookies": int(accounts_row.active_accounts or 0),
                "total_keywords": total_keywords,
                "total_orders": int(orders_row.total_orders or 0),
                "today_reply_count": int(reply_stats["today_reply_count"]),
                "yesterday_reply_count": int(reply_stats["yesterday_reply_count"]),
                "total_cards": total_cards,
                "password_configured": int(accounts_row.password_configured or 0),
            },
            "today_stats": {
                "today_users": today_users,
                "today_accounts": today_accounts,
                "today_orders": int(today_order_summary["today_orders"]),
                "today_shipped": int(today_order_summary["today_shipped"]),
                "today_pending": int(today_order_summary["today_pending"]),
                "today_amount": round(float(today_order_summary["today_amount"]), 2),
                "today_agent_orders": today_agent_orders,
            },
        }

    async def _get_admin_dashboard_bundle(self) -> dict[str, dict[str, int | float]]:
        return await DashboardStatsCacheService.get_admin_bundle(self._load_admin_dashboard_bundle)

    async def get_account_dashboard_stats(
        self,
        *,
        current_user_id: int,
        account_scope_owner_id: int | None,
        reply_scope_owner_id: int | None,
    ) -> dict[str, int | None]:
        """获取首页基础统计。"""
        account_stmt = select(
            func.count().label("total_accounts"),
            func.coalesce(
                func.sum(
                    case(
                        (self._build_enabled_account_condition(), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("active_accounts"),
        ).select_from(XYAccount)
        if account_scope_owner_id is not None:
            account_stmt = account_stmt.where(XYAccount.owner_id == account_scope_owner_id)
        account_row = (await self.session.execute(account_stmt)).one()

        keyword_stmt = (
            select(func.count())
            .select_from(XYKeywordRule)
            .join(XYAccount, XYKeywordRule.account_pk == XYAccount.id)
            .where(XYKeywordRule.is_active.is_(True))
        )
        if account_scope_owner_id is not None:
            keyword_stmt = keyword_stmt.where(XYAccount.owner_id == account_scope_owner_id)
        total_keywords = int((await self.session.execute(keyword_stmt)).scalar() or 0)

        order_stmt = select(func.count()).select_from(XYOrder)
        if account_scope_owner_id is not None:
            order_stmt = order_stmt.where(XYOrder.owner_id == account_scope_owner_id)
        total_orders = int((await self.session.execute(order_stmt)).scalar() or 0)

        limit_status = await self._get_limit_status(current_user_id)
        reply_stats = await AutoReplyStatsService(self.session).get_today_and_yesterday_success_reply_counts(
            reply_scope_owner_id
        )

        return {
            "total_accounts": int(account_row.total_accounts or 0),
            "active_accounts": int(account_row.active_accounts or 0),
            "total_keywords": total_keywords,
            "total_orders": total_orders,
            "today_reply_count": int(reply_stats["today_reply_count"]),
            "yesterday_reply_count": int(reply_stats["yesterday_reply_count"]),
            "account_limit": limit_status["account_limit"],
            "used_account_count": int(limit_status["used_count"]),
            "remaining_account_count": limit_status["remaining_count"],
        }

    async def _get_online_cookies_count(self) -> int:
        """实时获取真实 WebSocket 在线账号数（10 秒 TTL 缓存）。

        失败时返回 0，不影响其它统计展示。
        """
        now = time.time()
        if self.__class__._online_cookies_cache is not None:
            ts, count = self.__class__._online_cookies_cache
            if now - ts < self._ONLINE_COOKIES_CACHE_TTL:
                return count

        try:
            settings = get_settings()
            url = f"{settings.websocket_service_url.rstrip('/')}/internal/accounts/connection-stats"
            response = await get_http_client().get(url)
            if isinstance(response, dict) and response.get("success"):
                count = int((response.get("data") or {}).get("connected", 0) or 0)
                self.__class__._online_cookies_cache = (now, count)
                return count
        except Exception as e:
            logger.warning(f"获取在线账号数失败: {e}")
        return 0

    async def get_admin_dashboard_stats(self, *, current_user_id: int) -> dict[str, int | None]:
        """获取管理员首页全局统计。"""
        bundle = await self._get_admin_dashboard_bundle()
        limit_status = await self._get_limit_status(current_user_id)
        admin_stats = bundle["admin_stats"]
        online_cookies = await self._get_online_cookies_count()

        return {
            "total_users": int(admin_stats["total_users"]),
            "total_cookies": int(admin_stats["total_cookies"]),
            "active_cookies": int(admin_stats["active_cookies"]),
            "online_cookies": online_cookies,
            "total_keywords": int(admin_stats["total_keywords"]),
            "total_orders": int(admin_stats["total_orders"]),
            "today_reply_count": int(admin_stats["today_reply_count"]),
            "yesterday_reply_count": int(admin_stats["yesterday_reply_count"]),
            "total_cards": int(admin_stats["total_cards"]),
            "password_configured": int(admin_stats["password_configured"]),
            "current_user_account_limit": limit_status["account_limit"],
            "current_user_used_account_count": int(limit_status["used_count"]),
            "current_user_remaining_account_count": limit_status["remaining_count"],
        }

    async def get_admin_today_stats(self) -> dict[str, int | float]:
        """获取管理员今日统计。"""
        bundle = await self._get_admin_dashboard_bundle()
        today_stats = bundle["today_stats"]

        return {
            "today_users": int(today_stats["today_users"]),
            "today_accounts": int(today_stats["today_accounts"]),
            "today_orders": int(today_stats["today_orders"]),
            "today_shipped": int(today_stats["today_shipped"]),
            "today_pending": int(today_stats["today_pending"]),
            "today_amount": round(float(today_stats["today_amount"]), 2),
            "today_agent_orders": int(today_stats["today_agent_orders"]),
        }

    async def get_order_amount_trend(self, *, owner_id: int | None, days: int = 30) -> list[dict[str, int | float | str]]:
        """获取近N天订单金额趋势。"""
        start_date = self._build_today_start() - timedelta(days=days - 1)

        # 只按真实下单时间(placed_at)统计趋势，不对 created_at 做回退，
        # 避免同步历史订单时 created_at=今天被误算到今日曲线
        placed_stmt = (
            select(
                func.date(XYOrder.placed_at).label("order_date"),
                func.coalesce(func.sum(XYOrder.amount), 0).label("daily_amount"),
                func.count().label("daily_count"),
            )
            .select_from(XYOrder)
            .where(
                XYOrder.placed_at >= start_date,
                XYOrder.status.notin_(self.CLOSED_ORDER_STATUSES),
            )
            .group_by(func.date(XYOrder.placed_at))
            .order_by(func.date(XYOrder.placed_at))
        )
        if owner_id is not None:
            placed_stmt = placed_stmt.where(XYOrder.owner_id == owner_id)

        placed_rows = (await self.session.execute(placed_stmt)).all()

        amount_map: dict[str, float] = {}
        count_map: dict[str, int] = {}
        self._merge_trend_rows(placed_rows, amount_map, count_map)

        trend_data: list[dict[str, int | float | str]] = []
        for index in range(days):
            current_day = start_date + timedelta(days=index)
            date_key = current_day.strftime("%m-%d")
            trend_data.append(
                {
                    "date": date_key,
                    "amount": round(amount_map.get(date_key, 0), 2),
                    "count": count_map.get(date_key, 0),
                }
            )
        return trend_data

    async def get_delivery_stats(
        self,
        period: str = "today",
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """获取发货统计

        Args:
            period: 时间范围 (today/yesterday/7days/30days)
            owner_id: 用户ID（None表示管理员查询全部）

        Returns:
            发货统计字典，包含：
            - total_deliveries: 总发货数
            - successful: 成功发货数
            - failed: 失败发货数
            - success_rate: 成功率
            - avg_delivery_time: 平均发货时间（秒）
            - failure_reasons: 失败原因分布
            - hourly_volume: 每小时发货量（用于图表）
        """
        from common.models.auto_reply_message_log import XYAutoReplyMessageLog
        from sqlalchemy import case, extract

        # 计算时间范围
        now = datetime.now(BEIJING_TZ)
        if period == "today":
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "yesterday":
            start_time = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "7days":
            start_time = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "30days":
            start_time = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # 基础查询条件
        base_conditions = [
            XYAutoReplyMessageLog.created_at >= start_time.replace(tzinfo=None),
        ]
        if period == "yesterday":
            base_conditions.append(XYAutoReplyMessageLog.created_at < end_time.replace(tzinfo=None))
        if owner_id is not None:
            base_conditions.append(XYAutoReplyMessageLog.owner_id == owner_id)

        # 统计总发货数、成功数、失败数
        stats_stmt = (
            select(
                func.count().label("total"),
                func.coalesce(
                    func.sum(case(
                        (XYAutoReplyMessageLog.send_status == "success", 1),
                        else_=0,
                    )),
                    0,
                ).label("successful"),
                func.coalesce(
                    func.sum(case(
                        (XYAutoReplyMessageLog.send_status == "failed", 1),
                        else_=0,
                    )),
                    0,
                ).label("failed"),
            )
            .select_from(XYAutoReplyMessageLog)
            .where(and_(*base_conditions))
        )
        stats_row = (await self.session.execute(stats_stmt)).one()

        total = int(stats_row.total or 0)
        successful = int(stats_row.successful or 0)
        failed = int(stats_row.failed or 0)
        success_rate = round(successful / total * 100, 2) if total > 0 else 0.0

        # 失败原因分布
        failure_stmt = (
            select(
                XYAutoReplyMessageLog.send_fail_reason,
                func.count().label("count"),
            )
            .select_from(XYAutoReplyMessageLog)
            .where(
                and_(
                    *base_conditions,
                    XYAutoReplyMessageLog.send_status == "failed",
                    XYAutoReplyMessageLog.send_fail_reason.isnot(None),
                )
            )
            .group_by(XYAutoReplyMessageLog.send_fail_reason)
            .order_by(func.count().desc())
            .limit(10)
        )
        failure_rows = (await self.session.execute(failure_stmt)).all()
        failure_reasons = [
            {"reason": row[0] or "未知原因", "count": int(row[1])}
            for row in failure_rows
        ]

        # 每小时发货量（用于图表）
        hourly_stmt = (
            select(
                extract("hour", XYAutoReplyMessageLog.created_at).label("hour"),
                func.count().label("count"),
            )
            .select_from(XYAutoReplyMessageLog)
            .where(and_(*base_conditions))
            .group_by(extract("hour", XYAutoReplyMessageLog.created_at))
            .order_by(extract("hour", XYAutoReplyMessageLog.created_at))
        )
        hourly_rows = (await self.session.execute(hourly_stmt)).all()
        hourly_volume = [
            {"hour": int(row[0]), "count": int(row[1])}
            for row in hourly_rows
        ]

        # 填充缺失的小时（0-23）
        hourly_map = {item["hour"]: item["count"] for item in hourly_volume}
        hourly_volume = [
            {"hour": h, "count": hourly_map.get(h, 0)}
            for h in range(24)
        ]

        # 计算平均发货时间（基于 created_at 到 updated_at 的差值）
        # 注意：这是一个估算，因为没有精确的"发货完成时间"字段
        avg_time_stmt = (
            select(
                func.avg(
                    func.TIMESTAMPDIFF(
                        text('SECOND'),
                        XYAutoReplyMessageLog.created_at,
                        XYAutoReplyMessageLog.updated_at,
                    )
                ).label("avg_seconds"),
            )
            .select_from(XYAutoReplyMessageLog)
            .where(
                and_(
                    *base_conditions,
                    XYAutoReplyMessageLog.send_status == "success",
                )
            )
        )
        avg_time_result = (await self.session.execute(avg_time_stmt)).scalar()
        avg_delivery_time = round(float(avg_time_result or 0), 2)

        return {
            "total_deliveries": total,
            "successful": successful,
            "failed": failed,
            "success_rate": success_rate,
            "avg_delivery_time": avg_delivery_time,
            "failure_reasons": failure_reasons,
            "hourly_volume": hourly_volume,
        }

    async def get_delivery_alerts(self) -> list[dict[str, Any]]:
        """获取发货相关告警

        Returns:
            告警列表，每条告警包含：
            - alert_type: 告警类型
            - severity: 严重程度 (info/warning/critical)
            - message: 告警消息
            - created_at: 告警时间
        """
        from common.models.auto_reply_message_log import XYAutoReplyMessageLog
        from common.models.card import Card

        alerts = []

        # 1. 检查今日失败率
        today_start = self._build_today_start()
        today_stats_stmt = (
            select(
                func.count().label("total"),
                func.coalesce(
                    func.sum(case(
                        (XYAutoReplyMessageLog.send_status == "failed", 1),
                        else_=0,
                    )),
                    0,
                ).label("failed"),
            )
            .select_from(XYAutoReplyMessageLog)
            .where(XYAutoReplyMessageLog.created_at >= today_start)
        )
        today_row = (await self.session.execute(today_stats_stmt)).one()
        today_total = int(today_row.total or 0)
        today_failed = int(today_row.failed or 0)

        if today_total > 0:
            failure_rate = today_failed / today_total * 100
            if failure_rate >= 30:
                alerts.append({
                    "alert_type": "high_failure_rate",
                    "severity": "critical",
                    "message": f"今日发货失败率过高: {failure_rate:.1f}% ({today_failed}/{today_total})",
                    "created_at": datetime.now(BEIJING_TZ).isoformat(),
                })
            elif failure_rate >= 15:
                alerts.append({
                    "alert_type": "elevated_failure_rate",
                    "severity": "warning",
                    "message": f"今日发货失败率偏高: {failure_rate:.1f}% ({today_failed}/{today_total})",
                    "created_at": datetime.now(BEIJING_TZ).isoformat(),
                })

        # 2. 检查卡券库存（检查关联到商品的卡券）
        low_inventory_stmt = (
            select(
                Card.id,
                Card.name,
                Card.item_id,
                Card.delivery_count,
            )
            .select_from(Card)
            .where(
                Card.enabled == True,
                Card.item_id.isnot(None),
            )
            .order_by(Card.delivery_count.desc())
            .limit(100)
        )
        low_inventory_rows = (await self.session.execute(low_inventory_stmt)).all()

        # 注意：这里只能检查发货次数，如果需要精确库存需要检查 data_content 中的剩余数量
        # 简单检查：如果卡券的 data_content 为空或很短，可能库存不足
        for row in low_inventory_rows:
            card_id, card_name, item_id, delivery_count = row
            # 获取卡券详情检查库存
            card_detail_stmt = select(Card.data_content).where(Card.id == card_id)
            card_content = (await self.session.execute(card_detail_stmt)).scalar()

            if card_content:
                # 计算剩余库存（简单估算：按行数计算）
                remaining = card_content.count("\n") + 1 if card_content.strip() else 0
                if remaining <= 5 and remaining > 0:
                    alerts.append({
                        "alert_type": "low_inventory",
                        "severity": "warning",
                        "message": f"卡券 '{card_name}' 库存不足: 剩余约 {remaining} 个",
                        "created_at": datetime.now(BEIJING_TZ).isoformat(),
                        "card_id": card_id,
                        "item_id": item_id,
                    })
                elif remaining == 0:
                    alerts.append({
                        "alert_type": "out_of_stock",
                        "severity": "critical",
                        "message": f"卡券 '{card_name}' 已售罄",
                        "created_at": datetime.now(BEIJING_TZ).isoformat(),
                        "card_id": card_id,
                        "item_id": item_id,
                    })

        # 3. 检查最近1小时是否有大量失败
        one_hour_ago = datetime.now(BEIJING_TZ) - timedelta(hours=1)
        recent_failures_stmt = (
            select(func.count())
            .select_from(XYAutoReplyMessageLog)
            .where(
                XYAutoReplyMessageLog.created_at >= one_hour_ago.replace(tzinfo=None),
                XYAutoReplyMessageLog.send_status == "failed",
            )
        )
        recent_failures = int((await self.session.execute(recent_failures_stmt)).scalar() or 0)

        if recent_failures >= 10:
            alerts.append({
                "alert_type": "recent_spike_failures",
                "severity": "critical",
                "message": f"最近1小时发货失败 {recent_failures} 次，可能存在系统问题",
                "created_at": datetime.now(BEIJING_TZ).isoformat(),
            })

        # 按严重程度排序
        severity_order = {"critical": 0, "warning": 1, "info": 2}
        alerts.sort(key=lambda x: severity_order.get(x["severity"], 99))

        return alerts
