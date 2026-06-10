"""
卡券库存监控定时任务

功能：
1. 定期检查所有 data 类型卡券的剩余库存
2. 库存低于阈值时发送飞书卡片通知
3. 使用 Redis 冷却机制避免重复告警
"""
from __future__ import annotations

import asyncio

from loguru import logger

from common.db.session import async_session_maker
from common.models.notification_channel import NotificationChannel
from common.services.inventory_monitor import inventory_monitor
from sqlalchemy import select


class InventoryMonitorTask:
    """卡券库存监控定时任务"""

    def __init__(self):
        self.task_name = "卡券库存监控"
        self._lock = asyncio.Lock()

    async def execute(self) -> None:
        """执行一次库存检查

        若已有检查在执行中，则跳过本次。
        """
        if self._lock.locked():
            logger.debug(f"【{self.task_name}】已有检查正在执行，跳过本次触发")
            return
        async with self._lock:
            await self._run_check()

    async def _run_check(self) -> None:
        """实际执行库存检查"""
        logger.info(f"【{self.task_name}】开始执行")

        try:
            # 查找飞书通知渠道的 webhook URL
            webhook_url = await self._get_feishu_webhook()

            async with async_session_maker() as session:
                alert_count = await inventory_monitor.check_and_notify(
                    session=session,
                    webhook_url=webhook_url,
                    threshold=5,  # 默认阈值，卡券可单独覆盖
                )

            if alert_count > 0:
                logger.info(f"【{self.task_name}】发送了 {alert_count} 个库存告警")
            else:
                logger.info(f"【{self.task_name}】检查完成，库存充足")

        except Exception as e:
            logger.error(f"【{self.task_name}】执行异常: {e}")

    @staticmethod
    async def _get_feishu_webhook() -> str:
        """获取飞书通知渠道的 webhook URL

        Returns:
            飞书 webhook URL，未配置时返回空字符串
        """
        try:
            async with async_session_maker() as session:
                stmt = select(NotificationChannel).where(
                    NotificationChannel.channel_type == "feishu",
                    NotificationChannel.enabled == True,
                )
                result = await session.execute(stmt)
                channel = result.scalars().first()

                if channel and channel.config_payload:
                    config = channel.config_payload
                    if isinstance(config, str):
                        import json
                        config = json.loads(config)
                    return config.get("webhook_url", "")
        except Exception as e:
            logger.warning(f"获取飞书通知渠道失败: {e}")
        return ""


# 模块级单例
inventory_monitor_task_service = InventoryMonitorTask()
