"""
卡券库存监控服务

功能：
1. 定期检查卡券库存（data 类型卡券的剩余数据行数）
2. 库存低于阈值时触发飞书卡片通知
3. 使用 Redis 记录已发送的告警，避免重复通知
4. 支持每张卡券单独配置预警阈值
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from common.db.redis_client import get_redis_client
from common.models.card import Card
from common.utils.feishu_card import FeishuCardBuilder
from common.utils.notification_utils import send_feishu_card

# Redis key 前缀：存储已发送的库存告警
_ALERT_KEY_PREFIX = "inventory:alert:"

# 告警冷却时间（秒）：同一张卡券在冷却期内不重复告警
_DEFAULT_ALERT_COOLDOWN = 3600  # 1 小时


class InventoryMonitor:
    """卡券库存监控器

    检查所有 data 类型的卡券，当剩余数据行数低于阈值时触发通知。
    使用 Redis 避免对同一张卡券重复发送告警。
    """

    def __init__(self, cooldown_seconds: int = _DEFAULT_ALERT_COOLDOWN):
        """
        Args:
            cooldown_seconds: 告警冷却时间（秒），同一卡券在冷却期内不重复告警
        """
        self._cooldown_seconds = cooldown_seconds

    @staticmethod
    def _count_remaining(card: Card) -> int:
        """计算 data 类型卡券的剩余数据条数

        Args:
            card: 卡券对象

        Returns:
            剩余数据行数；非 data 类型返回 -1
        """
        if card.type != "data":
            return -1
        if not card.data_content:
            return 0
        lines = [line.strip() for line in card.data_content.split("\n") if line.strip()]
        return len(lines)

    @staticmethod
    def _get_threshold(card: Card, default_threshold: int) -> int:
        """获取卡券的预警阈值

        优先使用卡券描述中配置的阈值（格式: threshold=N），
        如果未配置则使用默认阈值。

        Args:
            card: 卡券对象
            default_threshold: 默认阈值

        Returns:
            预警阈值
        """
        # 从 description 中解析阈值配置
        if card.description:
            for part in card.description.split(";"):
                part = part.strip()
                if part.startswith("threshold="):
                    try:
                        return int(part.split("=", 1)[1])
                    except (ValueError, IndexError):
                        pass
        return default_threshold

    async def _is_alert_sent(self, card_id: int) -> bool:
        """检查该卡券是否已在冷却期内发送过告警

        Args:
            card_id: 卡券ID

        Returns:
            True 表示已发送过（在冷却期内），不应重复发送
        """
        try:
            client = await get_redis_client()
            key = f"{_ALERT_KEY_PREFIX}{card_id}"
            value = await client.get(key)
            return value is not None
        except Exception as e:
            logger.warning(f"库存监控 Redis 查询失败: {e}")
            return False

    async def _mark_alert_sent(self, card_id: int) -> None:
        """标记该卡券的告警已发送（设置冷却过期）

        Args:
            card_id: 卡券ID
        """
        try:
            client = await get_redis_client()
            key = f"{_ALERT_KEY_PREFIX}{card_id}"
            await client.setex(key, self._cooldown_seconds, "1")
        except Exception as e:
            logger.warning(f"库存监控 Redis 写入失败: {e}")

    async def _clear_alert(self, card_id: int) -> None:
        """清除卡券的告警标记（库存恢复后）

        Args:
            card_id: 卡券ID
        """
        try:
            client = await get_redis_client()
            key = f"{_ALERT_KEY_PREFIX}{card_id}"
            await client.delete(key)
        except Exception as e:
            logger.warning(f"库存监控 Redis 删除失败: {e}")

    async def check_low_inventory(
        self,
        session,
        card_items: Optional[List[Dict[str, Any]]] = None,
        threshold: int = 5,
    ) -> List[Dict[str, Any]]:
        """检查低库存卡券

        Args:
            session: 异步数据库会话
            card_items: 卡券字典列表（若为 None 则从数据库查询所有 data 类型卡券）
            threshold: 默认预警阈值

        Returns:
            低库存卡券列表，每项包含:
            - card_id: 卡券ID
            - card_name: 卡券名称
            - remaining: 剩余数量
            - threshold: 预警阈值
        """
        low_stock_items = []

        if card_items is None:
            # 从数据库查询所有启用的 data 类型卡券
            stmt = select(Card).where(
                Card.enabled == True,
                Card.type == "data",
            )
            result = await session.execute(stmt)
            cards = list(result.scalars().all())
        else:
            # 从传入的卡券字典列表构造查询
            card_ids = [item.get("id") for item in card_items if item.get("id")]
            if not card_ids:
                return []
            stmt = select(Card).where(Card.id.in_(card_ids), Card.type == "data")
            result = await session.execute(stmt)
            cards = list(result.scalars().all())

        for card in cards:
            remaining = self._count_remaining(card)
            if remaining < 0:
                continue  # 非 data 类型，跳过

            card_threshold = self._get_threshold(card, threshold)

            if remaining <= card_threshold:
                low_stock_items.append({
                    "card_id": card.id,
                    "card_name": card.name,
                    "remaining": remaining,
                    "threshold": card_threshold,
                    "user_id": card.user_id,
                })

        return low_stock_items

    async def check_and_notify(
        self,
        session,
        webhook_url: Optional[str] = None,
        threshold: int = 5,
    ) -> int:
        """检查库存并发送告警通知

        完整流程：
        1. 查询所有低库存卡券
        2. 过滤已发送过告警的卡券（Redis 冷却）
        3. 为每个低库存卡券发送飞书卡片通知
        4. 标记已发送告警

        Args:
            session: 异步数据库会话
            webhook_url: 飞书 webhook 地址（为 None 时只检查不通知）
            threshold: 默认预警阈值

        Returns:
            发送告警的数量
        """
        low_stock_items = await self.check_low_inventory(session, threshold=threshold)

        if not low_stock_items:
            logger.debug("库存监控：所有卡券库存充足")
            return 0

        logger.info(f"库存监控：发现 {len(low_stock_items)} 个低库存卡券")

        alert_count = 0
        for item in low_stock_items:
            card_id = item["card_id"]

            # 检查冷却期
            if await self._is_alert_sent(card_id):
                logger.debug(f"库存监控：卡券 {item['card_name']} (ID={card_id}) 已在冷却期内，跳过")
                continue

            logger.warning(
                f"库存监控：卡券 {item['card_name']} (ID={card_id}) "
                f"库存不足，剩余 {item['remaining']}，阈值 {item['threshold']}"
            )

            # 发送通知
            if webhook_url:
                try:
                    card_data = FeishuCardBuilder.build_inventory_alert_card(
                        card_name=item["card_name"],
                        remaining_count=item["remaining"],
                        threshold=item["threshold"],
                    )
                    success = await send_feishu_card(webhook_url, card_data)
                    if success:
                        alert_count += 1
                except Exception as e:
                    logger.error(f"库存监控：发送告警通知失败 (卡券 {item['card_name']}): {e}")

            # 标记已发送（无论是否成功，避免频繁重试）
            await self._mark_alert_sent(card_id)

        return alert_count


# 模块级单例
inventory_monitor = InventoryMonitor()
