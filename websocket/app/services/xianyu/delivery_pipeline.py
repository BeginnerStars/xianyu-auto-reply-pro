"""
发货管道模块 (Delivery Pipeline)
=================================
将 AutoDeliveryHandler._handle_auto_delivery 的 600+ 行流程
重构为清晰的管道模式（Pipeline Pattern）。

每个步骤是一个独立方法，接收 DeliveryContext 并返回 DeliveryResult。
当 DeliveryResult.should_stop 为 True 时，管道立即停止。

本模块不修改原 auto_delivery_handler.py，可作为未来替换的候选方案。

用法示例::

    pipeline = DeliveryPipeline(handler)
    ctx = DeliveryContext(websocket=ws, message=msg, ...)
    result = await pipeline.execute(ctx)
    if result.success:
        ...
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger


# ============================================================
# 数据结构
# ============================================================


@dataclass
class DeliveryContext:
    """发货管道的完整上下文，贯穿所有步骤。"""

    # ---- 入参：调用方传入 ----
    websocket: Any
    """WebSocket 连接对象"""
    message: dict
    """原始推送消息"""
    send_user_name: str
    """买家昵称（可能为系统占位文案）"""
    send_user_id: str
    """买家用户 ID"""
    item_id: str
    """商品 ID"""
    chat_id: str
    """聊天会话 ID"""
    msg_time: str
    """消息时间戳（用于日志）"""
    override_order_id: Optional[str] = None
    """重发货场景下外部指定的订单 ID"""
    pre_check_result: Optional[dict] = None
    """外部已预先调用 pre_delivery_check_and_close 的结果"""

    # ---- 管道内部填充 ----
    order_id: Optional[str] = None
    """提取到的订单 ID"""
    user_url: str = ""
    """买家主页 URL"""
    local_buyer_fish_nick: Optional[str] = None
    """买家明文昵称（pre_check 阶段获取）"""
    skip_confirm_for_card_only: bool = False
    """是否为 card_only 模式（仅发卡券、跳过确认发货接口）"""

    # 数量相关
    quantity_to_send: int = 1
    """需要发送的卡券数量"""
    multi_quantity_delivery: bool = False
    """是否启用了多数量发货"""

    # 锁相关
    lock_key: Optional[str] = None
    """分布式锁的键"""
    lock_result: Any = None
    """Redis 锁返回值"""
    redis_lock_acquired: bool = False
    """是否已获取 Redis 分布式锁"""

    # 卡券 & 规则
    card: Optional[dict] = None
    """匹配到的卡券对象"""
    rule: Optional[dict] = None
    """与旧格式兼容的规则字典"""
    spec_name: Optional[str] = None
    """多规格-规格名"""
    spec_value: Optional[str] = None
    """多规格-规格值"""

    # 发货内容
    delivery_contents: List[str] = field(default_factory=list)
    """所有获取到的发货内容"""
    success_count: int = 0
    """成功获取内容的次数"""
    order_already_shipped: bool = False
    """订单是否已发货"""
    quantity_degraded_for_dock: bool = False
    """因对接卡券退化为 1 张"""
    quantity_degraded_for_fixed_content: bool = False
    """因固定内容退化为 1 张"""

    # 发送结果
    send_results: List[dict] = field(default_factory=list)
    """消息发送结果列表"""
    any_send_failed: bool = False
    """是否有消息发送失败"""

    # 确认发货
    send_before_confirm_fail_msg: Optional[str] = None
    """send_before_confirm 模式下确认发货失败原因"""


@dataclass
class DeliveryResult:
    """步骤执行结果。"""

    success: bool
    """步骤是否成功"""
    should_stop: bool = False
    """管道是否应在此步骤停止"""
    reason: str = ""
    """失败原因或说明"""
    metadata: Dict[str, Any] = field(default_factory=dict)
    """额外元数据"""

    @classmethod
    def ok(cls, metadata: Optional[Dict[str, Any]] = None) -> "DeliveryResult":
        return cls(success=True, metadata=metadata or {})

    @classmethod
    def stop(cls, reason: str = "", metadata: Optional[Dict[str, Any]] = None) -> "DeliveryResult":
        return cls(success=False, should_stop=True, reason=reason, metadata=metadata or {})


# ============================================================
# 管道
# ============================================================

class DeliveryPipeline:
    """发货管道，将 _handle_auto_delivery 拆分为独立步骤。

    Args:
        handler: AutoDeliveryHandler 实例，管道通过它访问所有业务能力。
    """

    def __init__(self, handler: Any):
        self.handler = handler

    # ---- 辅助属性代理 ----

    @property
    def cookie_id(self) -> str:
        return self.handler.cookie_id

    def _safe_str(self, obj: Any) -> str:
        return self.handler._safe_str(obj)

    # ============================================================
    # 主入口
    # ============================================================

    async def execute(self, ctx: DeliveryContext) -> DeliveryResult:
        """按序执行所有管道步骤，任一步骤 should_stop 则停止。

        返回最终的 DeliveryResult。
        """
        steps = [
            ("validate_order", self.step_validate_order),
            ("check_rules", self.step_check_rules),
            ("acquire_lock", self.step_acquire_lock),
            ("fetch_card_content", self.step_fetch_card_content),
            ("send_delivery_message", self.step_send_delivery_message),
            ("confirm_delivery", self.step_confirm_delivery),
            ("update_status", self.step_update_status),
            ("send_notification", self.step_send_notification),
        ]

        final_result: DeliveryResult = DeliveryResult.ok()

        for step_name, step_fn in steps:
            try:
                result = await step_fn(ctx)
                logger.debug(
                    f"【{self.cookie_id}】管道步骤 [{step_name}]: "
                    f"success={result.success}, should_stop={result.should_stop}"
                )
                if not result.success:
                    final_result = result
                if result.should_stop:
                    logger.info(
                        f"【{self.cookie_id}】管道在步骤 [{step_name}] 停止: {result.reason}"
                    )
                    return final_result if not final_result.success else result
            except Exception as e:
                err_msg = f"管道步骤 [{step_name}] 异常: {self._safe_str(e)}"
                logger.error(f"【{self.cookie_id}】{err_msg}")
                return DeliveryResult.stop(reason=err_msg)

        # 管道全部通过，处理发送失败的后续逻辑
        await self._handle_post_pipeline(ctx)

        return DeliveryResult.ok(metadata={
            "order_id": ctx.order_id,
            "delivery_count": len(ctx.delivery_contents),
            "any_send_failed": ctx.any_send_failed,
        })

    # ============================================================
    # 步骤 A：校验订单
    # ============================================================

    async def step_validate_order(self, ctx: DeliveryContext) -> DeliveryResult:
        """校验订单：商品归属、订单 ID 提取、金额检查。

        填充: ctx.order_id, ctx.user_url
        """
        try:
            # A1. 检查商品归属
            if ctx.item_id and ctx.item_id != "未知商品":
                try:
                    from common.db.compat import db_manager
                    item_info = db_manager.get_item_info(self.cookie_id, ctx.item_id)
                    if not item_info:
                        logger.warning(
                            f'[{ctx.msg_time}] 【{self.cookie_id}】'
                            f'❌ 商品 {ctx.item_id} 不属于当前账号，跳过自动发货'
                        )
                        return DeliveryResult.stop("商品不属于当前账号")
                    logger.warning(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'✅ 商品 {ctx.item_id} 归属验证通过'
                    )
                except Exception as e:
                    logger.error(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'检查商品归属失败: {self._safe_str(e)}，跳过自动发货'
                    )
                    return DeliveryResult.stop("检查商品归属失败")

            # A2. 提取订单 ID
            if ctx.override_order_id:
                ctx.order_id = ctx.override_order_id
                logger.info(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'使用指定订单ID: {ctx.order_id}（重发货触发）'
                )
            else:
                ctx.order_id = self.handler.parent._extract_order_id(ctx.message)

            if not ctx.order_id:
                logger.warning(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'❌ 未能提取到订单ID，跳过自动发货'
                )
                return DeliveryResult.stop("未能提取到订单ID")

            logger.info(
                f'[{ctx.msg_time}] 【{self.cookie_id}】'
                f'提取到订单ID: {ctx.order_id}，将在自动发货时处理确认发货'
            )

            # A3. 检查订单金额（为 0 禁止发货）
            try:
                from common.db.compat import db_manager
                order_check = db_manager.get_order_by_id(ctx.order_id)
                if order_check:
                    order_amount = order_check.get('amount')
                    if order_amount is not None:
                        from decimal import Decimal
                        if Decimal(str(order_amount)) <= 0:
                            logger.warning(
                                f'[{ctx.msg_time}] 【{self.cookie_id}】'
                                f'❌ 订单 {ctx.order_id} 金额为 {order_amount}，禁止自动发货'
                            )
                            await self.handler._update_delivery_fail_reason(
                                ctx.order_id, "账号已掉线，请重新登录"
                            )
                            return DeliveryResult.stop("订单金额为0")
            except Exception as e:
                logger.warning(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'检查订单金额异常: {self._safe_str(e)}'
                )

            # 构造用户 URL
            ctx.user_url = f'https://www.goofish.com/personal?userId={ctx.send_user_id}'
            ctx.lock_key = ctx.order_id

            return DeliveryResult.ok()

        except Exception as e:
            return DeliveryResult.stop(f"校验订单异常: {self._safe_str(e)}")

    # ============================================================
    # 步骤 B：规则引擎检查
    # ============================================================

    async def step_check_rules(self, ctx: DeliveryContext) -> DeliveryResult:
        """执行发货前规则引擎检查（pre_delivery_check_and_close）。

        填充: ctx.pre_check_result, ctx.skip_confirm_for_card_only,
              ctx.local_buyer_fish_nick
        """
        try:
            # 复用外部结果或自行调用
            if ctx.pre_check_result is not None:
                pre_check = ctx.pre_check_result
                logger.info(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】使用外部传入的 pre_check 结果，'
                    f'跳过内部重复检查: action={pre_check.get("action", "allow")}'
                )
            else:
                pre_check = await self.handler.pre_delivery_check_and_close(
                    websocket=ctx.websocket,
                    order_no=ctx.order_id,
                    buyer_id=ctx.send_user_id,
                    chat_id=ctx.chat_id,
                    log_prefix=f'[{ctx.msg_time}] 【{self.cookie_id}】',
                    item_id=ctx.item_id,
                )
            ctx.pre_check_result = pre_check

            action = pre_check.get('action', 'allow')
            if action == 'block':
                return DeliveryResult.stop("规则引擎拦截")

            ctx.skip_confirm_for_card_only = (action == 'card_only')
            ctx.local_buyer_fish_nick = (
                pre_check.get('buyer_fish_nick')
                or self.handler._current_buyer_fish_nick
            )

            return DeliveryResult.ok(metadata={"action": action})

        except Exception as e:
            return DeliveryResult.stop(f"规则引擎检查异常: {self._safe_str(e)}")

    # ============================================================
    # 步骤 C：获取分布式锁
    # ============================================================

    async def step_acquire_lock(self, ctx: DeliveryContext) -> DeliveryResult:
        """获取 Redis 分布式锁 + 多重冷却检查。

        填充: ctx.redis_lock_acquired, ctx.lock_result
        """
        try:
            lock_key = ctx.lock_key

            # C1. 延迟锁状态预检查
            if self.handler.is_lock_held(lock_key):
                logger.info(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'🔒【提前检查】订单 {lock_key} 延迟锁仍在持有状态，跳过发货'
                )
                return DeliveryResult.stop("延迟锁仍在持有")

            # C2. 时间冷却预检查
            if not self.handler.can_auto_delivery(ctx.order_id):
                logger.info(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'订单 {ctx.order_id} 在冷却期内，跳过发货'
                )
                return DeliveryResult.stop("在冷却期内")

            # C3. Redis 分布式锁
            from common.db.redis_client import try_acquire_delivery_lock

            try:
                ctx.lock_result = await try_acquire_delivery_lock(
                    ctx.order_id, expire=120,
                    holder_info=self.cookie_id, wait_timeout=5
                )
                if ctx.lock_result.success:
                    ctx.redis_lock_acquired = True
                    logger.info(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'获取Redis分布式锁成功: {ctx.order_id}'
                    )
                elif ctx.lock_result.is_locked_by_other:
                    logger.warning(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'❌ Redis分布式锁被其他进程持有，跳过发货: {ctx.order_id}'
                    )
                    return DeliveryResult.stop("Redis锁被其他进程持有")
                elif ctx.lock_result.has_error:
                    logger.warning(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'Redis连接异常，降级为本地锁控制: {ctx.order_id}'
                    )
            except Exception as e:
                logger.warning(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'Redis分布式锁异常，降级为本地锁控制: {ctx.order_id}, error={e}'
                )

            # C4. 获取锁后检查订单是否已发货
            if ctx.redis_lock_acquired and ctx.order_id:
                try:
                    from common.db.compat import db_manager
                    existing_order = db_manager.get_order_by_id(ctx.order_id)
                    if existing_order and existing_order.get('status') == 'shipped':
                        logger.info(
                            f'[{ctx.msg_time}] 【{self.cookie_id}】'
                            f'获取锁后检查发现订单 {ctx.order_id} 已发货，跳过处理'
                        )
                        return DeliveryResult.stop("订单已发货")
                except Exception as e:
                    logger.warning(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'获取锁后检查订单状态异常: {self._safe_str(e)}'
                    )

            # C5. 双重延迟锁检查
            if self.handler.is_lock_held(lock_key):
                logger.info(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'订单 {lock_key} 在获取锁后检查发现延迟锁仍持有，跳过发货'
                )
                return DeliveryResult.stop("延迟锁仍持有（双重检查）")

            # C6. 双重冷却检查
            if not self.handler.can_auto_delivery(ctx.order_id):
                logger.info(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'订单 {ctx.order_id} 在获取锁后检查发现仍在冷却期，跳过发货'
                )
                return DeliveryResult.stop("仍在冷却期（双重检查）")

            return DeliveryResult.ok()

        except Exception as e:
            return DeliveryResult.stop(f"获取锁异常: {self._safe_str(e)}")

    # ============================================================
    # 步骤 D：获取卡券内容
    # ============================================================

    async def step_fetch_card_content(self, ctx: DeliveryContext) -> DeliveryResult:
        """获取卡券内容（支持多数量、多类型）。

        填充: ctx.delivery_contents, ctx.success_count,
              ctx.order_already_shipped, ctx.quantity_to_send,
              ctx.quantity_degraded_for_dock, ctx.quantity_degraded_for_fixed_content
        """
        try:
            # D1. 重置状态
            self.handler._last_delivery_fail_reason = None
            self.handler._last_delivery_card_source = None
            self.handler._last_delivery_card_type = None
            item_title = "待获取商品信息"

            logger.info(
                f"【{self.cookie_id}】准备自动发货: item_id={ctx.item_id}, item_title={item_title}"
            )

            # D2. 多数量发货检测
            from common.db.compat import db_manager
            ctx.multi_quantity_delivery = db_manager.get_item_multi_quantity_delivery_status(
                self.cookie_id, ctx.item_id
            )

            if ctx.multi_quantity_delivery and ctx.order_id:
                logger.info(f"商品 {ctx.item_id} 开启了多数量发货，获取订单详情...")
                try:
                    order_detail = await self.handler.fetch_order_detail_info(
                        ctx.order_id, ctx.item_id, ctx.send_user_id
                    )
                    if order_detail and order_detail.get('quantity'):
                        try:
                            order_quantity = int(order_detail['quantity'])
                            if order_quantity > 1:
                                ctx.quantity_to_send = order_quantity
                                logger.info(f"从订单详情获取数量: {order_quantity}")
                        except (ValueError, TypeError):
                            logger.warning(f"订单数量格式无效，发送单个卡券")
                except Exception as e:
                    logger.error(f"获取订单详情失败: {self._safe_str(e)}")

            # D3. card_only 退化保护
            if ctx.quantity_to_send > 1 and ctx.skip_confirm_for_card_only:
                logger.warning(
                    f"【{self.cookie_id}】订单 {ctx.order_id} card_only 模式仅补发 1 张"
                )
                ctx.quantity_to_send = 1

            # D4. 循环获取发货内容
            for i in range(ctx.quantity_to_send):
                try:
                    delivery_content = await self.handler._auto_delivery(
                        ctx.item_id, item_title, ctx.order_id,
                        ctx.send_user_id, ctx.chat_id, ctx.send_user_name,
                        skip_confirm=ctx.skip_confirm_for_card_only,
                    )
                    if delivery_content:
                        ctx.delivery_contents.append(delivery_content)
                        ctx.success_count += 1
                        if ctx.quantity_to_send > 1:
                            logger.info(f"第 {i+1}/{ctx.quantity_to_send} 个卡券内容获取成功")

                        # 对接卡券退化
                        if ctx.quantity_to_send > 1 and self.handler._last_delivery_card_source in ('dock_l1', 'dock_l2'):
                            ctx.quantity_degraded_for_dock = True
                            logger.warning(
                                f"【{self.cookie_id}】订单 {ctx.order_id} 对接卡券暂不支持多数量发货，"
                                f"已退化为 1 张"
                            )
                            break

                        # 固定内容退化
                        if ctx.quantity_to_send > 1 and self.handler._last_delivery_card_type in ('text', 'image'):
                            ctx.quantity_degraded_for_fixed_content = True
                            logger.warning(
                                f"【{self.cookie_id}】订单 {ctx.order_id} 固定内容卡券退化为 1 张"
                            )
                            break

                    elif delivery_content is None and i == 0:
                        from common.db.compat import db_manager
                        existing_order = db_manager.get_order_by_id(ctx.order_id)
                        if existing_order and existing_order.get('status') == 'shipped':
                            logger.info(f"【{self.cookie_id}】订单 {ctx.order_id} 已发货，跳过")
                            ctx.order_already_shipped = True
                            break
                        else:
                            logger.warning(f"第 {i+1}/{ctx.quantity_to_send} 个卡券内容获取失败")
                    else:
                        logger.warning(f"第 {i+1}/{ctx.quantity_to_send} 个卡券内容获取失败")
                except Exception as e:
                    logger.error(f"第 {i+1}/{ctx.quantity_to_send} 个卡券获取异常: {self._safe_str(e)}")

            # D5. 判断结果
            if ctx.order_already_shipped:
                logger.info(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'订单 {ctx.order_id} 已发货，无需重复处理'
                )
                return DeliveryResult.stop("订单已发货", metadata={"already_shipped": True})

            if not ctx.delivery_contents:
                fail_msg = self.handler._last_delivery_fail_reason or "未找到匹配的发货规则或获取发货内容失败"
                return DeliveryResult.stop(fail_msg, metadata={"fail_reason": fail_msg})

            return DeliveryResult.ok(metadata={
                "delivery_count": len(ctx.delivery_contents),
                "quantity_to_send": ctx.quantity_to_send,
            })

        except Exception as e:
            return DeliveryResult.stop(f"获取卡券内容异常: {self._safe_str(e)}")

    # ============================================================
    # 步骤 E：发送发货消息
    # ============================================================

    async def step_send_delivery_message(self, ctx: DeliveryContext) -> DeliveryResult:
        """发送所有发货内容到买家。

        填充: ctx.send_results, ctx.any_send_failed
        """
        try:
            # 标记已发货 + 设置延迟锁
            self.handler.mark_delivery_sent(ctx.order_id)
            self.handler._lock_hold_info[ctx.lock_key] = {
                'locked': True,
                'lock_time': time.time(),
                'release_time': None,
                'task': None,
            }
            delay_task = asyncio.create_task(
                self.handler._delayed_lock_release(ctx.lock_key, delay_minutes=10)
            )
            self.handler._lock_hold_info[ctx.lock_key]['task'] = delay_task

            # 逐条发送
            for i, delivery_content in enumerate(ctx.delivery_contents):
                try:
                    await self._send_single_content(
                        ctx, i, delivery_content
                    )
                except Exception as e:
                    ctx.any_send_failed = True
                    logger.error(f"发送第 {i+1} 条消息失败: {self._safe_str(e)}")

            # 写消息日志
            await self.handler._record_delivery_log(
                chat_id=ctx.chat_id,
                item_id=ctx.item_id,
                sender_user_id=ctx.send_user_id,
                sender_user_name=ctx.send_user_name,
                msg_time=ctx.msg_time,
                order_id=ctx.order_id,
                delivery_contents=ctx.delivery_contents,
                send_results=ctx.send_results,
                any_send_failed=ctx.any_send_failed,
            )

            return DeliveryResult.ok(metadata={
                "any_send_failed": ctx.any_send_failed,
            })

        except Exception as e:
            return DeliveryResult.stop(f"发送消息异常: {self._safe_str(e)}")

    async def _send_single_content(
        self, ctx: DeliveryContext, index: int, delivery_content: str
    ) -> None:
        """发送单条发货内容（文本/图片/图文混合）。"""
        websocket = ctx.websocket
        send_results = ctx.send_results

        if delivery_content.startswith("__DELIVERY_WITH_IMAGES__"):
            await self._send_delivery_with_images(ctx, index, delivery_content)

        elif delivery_content.startswith("__MULTI_IMAGE_SEND__"):
            await self._send_multi_image(ctx, index, delivery_content)

        elif delivery_content.startswith("__IMAGE_SEND__"):
            await self._send_single_image(ctx, index, delivery_content)

        else:
            # 纯文本
            text_ok = await self.handler._send_text_with_separator(
                websocket, ctx.chat_id, ctx.send_user_id,
                delivery_content, ctx.msg_time, ctx.user_url,
                send_results=send_results,
            )
            if not text_ok:
                ctx.any_send_failed = True

            # 多数量间隔
            if len(ctx.delivery_contents) > 1 and index < len(ctx.delivery_contents) - 1:
                await asyncio.sleep(1)

    async def _send_delivery_with_images(
        self, ctx: DeliveryContext, index: int, delivery_content: str
    ) -> None:
        """发送 __DELIVERY_WITH_IMAGES__ 格式的内容。"""
        data = delivery_content.replace("__DELIVERY_WITH_IMAGES__", "")
        parts = data.split("|")
        if len(parts) < 3:
            logger.error(f"发货内容格式错误: {delivery_content[:100]}")
            return

        try:
            card_id = int(parts[0])
        except ValueError:
            card_id = None

        try:
            image_count = int(parts[1])
        except ValueError:
            image_count = 0

        image_urls = parts[2:2 + image_count] if image_count > 0 else []
        text_content = parts[2 + image_count] if len(parts) > 2 + image_count else ""

        # 发图片
        for img_idx, image_url in enumerate(image_urls):
            if image_url:
                img_result = await self.handler._send_image_msg_with_retry(
                    ctx.websocket, ctx.chat_id, ctx.send_user_id,
                    image_url, card_id=card_id, image_index=img_idx,
                )
                if isinstance(img_result, dict):
                    ctx.send_results.append(img_result)
                img_ok = isinstance(img_result, dict) and img_result.get("success", False)
                if img_ok:
                    logger.info(
                        f'[{ctx.msg_time}] 【自动发货图片】第 {img_idx+1}/{len(image_urls)} 张 '
                        f'已向 {ctx.user_url} 发送图片'
                    )
                else:
                    ctx.any_send_failed = True

        # 发文字
        if text_content:
            text_ok = await self.handler._send_text_with_separator(
                ctx.websocket, ctx.chat_id, ctx.send_user_id,
                text_content, ctx.msg_time, ctx.user_url,
                send_results=ctx.send_results,
            )
            if not text_ok:
                ctx.any_send_failed = True

    async def _send_multi_image(
        self, ctx: DeliveryContext, index: int, delivery_content: str
    ) -> None:
        """发送 __MULTI_IMAGE_SEND__ 格式的内容。"""
        image_data = delivery_content.replace("__MULTI_IMAGE_SEND__", "")
        parts = image_data.split("|")
        if len(parts) < 2:
            logger.error(f"多图片发送标记格式错误: {delivery_content}")
            return

        try:
            card_id = int(parts[0])
        except ValueError:
            card_id = None

        image_urls = parts[1:]
        for img_idx, image_url in enumerate(image_urls):
            if image_url:
                img_result = await self.handler._send_image_msg_with_retry(
                    ctx.websocket, ctx.chat_id, ctx.send_user_id,
                    image_url, card_id=card_id, image_index=img_idx,
                )
                if isinstance(img_result, dict):
                    ctx.send_results.append(img_result)
                img_ok = isinstance(img_result, dict) and img_result.get("success", False)
                if img_ok:
                    logger.info(
                        f'[{ctx.msg_time}] 【自动发货多图片】第 {img_idx+1}/{len(image_urls)} 张 '
                        f'已向 {ctx.user_url} 发送图片: {image_url}'
                    )
                else:
                    ctx.any_send_failed = True

    async def _send_single_image(
        self, ctx: DeliveryContext, index: int, delivery_content: str
    ) -> None:
        """发送 __IMAGE_SEND__ 格式的内容。"""
        image_data = delivery_content.replace("__IMAGE_SEND__", "")
        if "|" in image_data:
            card_id_str, image_url = image_data.split("|", 1)
            try:
                card_id = int(card_id_str)
            except ValueError:
                card_id = None
        else:
            card_id = None
            image_url = image_data

        img_result = await self.handler._send_image_msg_with_retry(
            ctx.websocket, ctx.chat_id, ctx.send_user_id,
            image_url, card_id=card_id,
        )
        if isinstance(img_result, dict):
            ctx.send_results.append(img_result)
        img_ok = isinstance(img_result, dict) and img_result.get("success", False)
        if img_ok:
            if len(ctx.delivery_contents) > 1:
                logger.info(
                    f'[{ctx.msg_time}] 【多数量自动发货图片】第 {index+1}/{len(ctx.delivery_contents)} 张 '
                    f'已向 {ctx.user_url} 发送图片: {image_url}'
                )
            else:
                logger.info(
                    f'[{ctx.msg_time}] 【自动发货图片】已向 {ctx.user_url} 发送图片: {image_url}'
                )
        else:
            ctx.any_send_failed = True

        if len(ctx.delivery_contents) > 1 and index < len(ctx.delivery_contents) - 1:
            await asyncio.sleep(1)

    # ============================================================
    # 步骤 F：确认发货
    # ============================================================

    async def step_confirm_delivery(self, ctx: DeliveryContext) -> DeliveryResult:
        """根据模式执行确认发货。

        - send_before_confirm 模式：卡券发送成功后再确认
        - card_only 模式：跳过确认
        - 其他：已在 _auto_delivery 内部完成
        """
        try:
            if ctx.skip_confirm_for_card_only:
                return DeliveryResult.ok(metadata={"skipped": True, "reason": "card_only"})

            if not ctx.any_send_failed and self.handler.is_send_before_confirm_enabled():
                # send_before_confirm 模式
                if ctx.order_id and not ctx.any_send_failed:
                    logger.info(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'卡券发送成功，开始执行确认发货: order_id={ctx.order_id}'
                    )
                    if self.handler.is_auto_confirm_enabled():
                        confirm_result = await self.handler.auto_confirm(
                            ctx.order_id, ctx.item_id
                        )
                        if confirm_result.get('success'):
                            logger.info(
                                f'[{ctx.msg_time}] 【{self.cookie_id}】'
                                f'🎉 卡券发送后确认发货成功: order_id={ctx.order_id}'
                            )
                        else:
                            confirm_error = confirm_result.get('error', '未知错误')
                            ctx.send_before_confirm_fail_msg = (
                                f"⚠️ 卡券已发送成功，但确认发货失败: {confirm_error}，请手动确认发货"
                            )
                            logger.warning(
                                f'[{ctx.msg_time}] 【{self.cookie_id}】'
                                f'{ctx.send_before_confirm_fail_msg}'
                            )
                            await self.handler.send_delivery_failure_notification(
                                ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                                ctx.send_before_confirm_fail_msg, ctx.chat_id,
                            )
                    else:
                        ctx.send_before_confirm_fail_msg = (
                            "⚠️ 卡券已发送成功，但自动确认发货已关闭，请手动确认发货"
                        )
                        logger.info(
                            f'[{ctx.msg_time}] 【{self.cookie_id}】'
                            f'自动确认发货已关闭: order_id={ctx.order_id}'
                        )

            elif self.handler.is_send_before_confirm_enabled() and ctx.any_send_failed:
                ctx.send_before_confirm_fail_msg = (
                    "⚠️ 卡券发送存在失败，已跳过确认发货，请检查买家是否收到完整内容后手动确认发货"
                )
                logger.warning(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'卡券发送存在失败，跳过确认发货: order_id={ctx.order_id}'
                )
                await self.handler.send_delivery_failure_notification(
                    ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                    ctx.send_before_confirm_fail_msg, ctx.chat_id,
                )

            return DeliveryResult.ok()

        except Exception as e:
            return DeliveryResult.stop(f"确认发货异常: {self._safe_str(e)}")

    # ============================================================
    # 步骤 G：更新订单状态
    # ============================================================

    async def step_update_status(self, ctx: DeliveryContext) -> DeliveryResult:
        """更新数据库中的订单状态。"""
        try:
            from common.services.order_service import OrderService
            from common.db.session import async_session_maker

            combined_content = (
                "\n---\n".join(ctx.delivery_contents)
                if len(ctx.delivery_contents) > 1
                else ctx.delivery_contents[0]
            )

            async with async_session_maker() as db_session:
                order_service = OrderService(db_session)

                if ctx.skip_confirm_for_card_only:
                    await order_service.record_delivery_for_closed_order(
                        order_no=ctx.order_id,
                        delivery_method="auto",
                        delivery_content=combined_content,
                        buyer_fish_nick=ctx.local_buyer_fish_nick,
                    )
                    logger.info(
                        f"【{self.cookie_id}】订单 {ctx.order_id} card_only 模式："
                        f"已记录补发卡券内容（订单状态保持已关闭）"
                    )
                else:
                    await order_service.update_order_delivery_info(
                        order_no=ctx.order_id,
                        status="shipped",
                        delivery_method="auto",
                        delivery_content=combined_content,
                        buyer_fish_nick=ctx.local_buyer_fish_nick,
                    )
                    logger.info(
                        f"【{self.cookie_id}】订单 {ctx.order_id} 状态已更新为已发货（自动发货）"
                    )

                # 退化提示写入
                degraded_warn_msg = None
                if ctx.quantity_degraded_for_dock:
                    remaining = max(ctx.quantity_to_send - len(ctx.delivery_contents), 0)
                    degraded_warn_msg = (
                        f"⚠️ 对接卡券暂不支持多数量发货：订单数量 {ctx.quantity_to_send} 张，"
                        f"已自动发送 {len(ctx.delivery_contents)} 张，"
                        f"剩余 {remaining} 张请手动补发或改用自有卡券"
                    )
                elif ctx.quantity_degraded_for_fixed_content:
                    remaining = max(ctx.quantity_to_send - len(ctx.delivery_contents), 0)
                    degraded_warn_msg = (
                        f"⚠️ 固定内容卡券（{self.handler._last_delivery_card_type} 类型）"
                        f"不支持多数量发货：订单数量 {ctx.quantity_to_send} 张，"
                        f"仅发送 1 张固定内容（剩余 {remaining} 张未发）。"
                        f"如需多数量发送不同卡密，请改用 data 或 api 类型卡券"
                    )

                if degraded_warn_msg:
                    try:
                        await order_service.update_order_delivery_fail_reason(
                            ctx.order_id, degraded_warn_msg
                        )
                    except Exception as _warn_err:
                        logger.warning(f"写入退化提示失败: {self._safe_str(_warn_err)}")

                # send_before_confirm 失败原因写入
                if ctx.send_before_confirm_fail_msg and not degraded_warn_msg:
                    try:
                        await order_service.update_order_delivery_fail_reason(
                            ctx.order_id, ctx.send_before_confirm_fail_msg
                        )
                    except Exception as _sbc_err:
                        logger.warning(f"写入确认发货失败原因失败: {self._safe_str(_sbc_err)}")
                elif ctx.send_before_confirm_fail_msg and degraded_warn_msg:
                    combined_reason = f"{degraded_warn_msg}；{ctx.send_before_confirm_fail_msg}"
                    try:
                        await order_service.update_order_delivery_fail_reason(
                            ctx.order_id, combined_reason
                        )
                    except Exception as _sbc_err:
                        logger.warning(f"写入合并失败原因失败: {self._safe_str(_sbc_err)}")

            return DeliveryResult.ok()

        except Exception as e:
            logger.error(f"【{self.cookie_id}】更新订单状态失败: {self._safe_str(e)}")
            return DeliveryResult.ok()  # 不阻断管道

    # ============================================================
    # 步骤 H：发送通知
    # ============================================================

    async def step_send_notification(self, ctx: DeliveryContext) -> DeliveryResult:
        """发送发货结果通知。"""
        try:
            if ctx.any_send_failed:
                fail_notify_msg = "部分发货消息发送失败（WebSocket连接断开），请检查买家是否收到完整内容"
                logger.error(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'订单 {ctx.order_id} {fail_notify_msg}'
                )
                await self.handler.send_delivery_failure_notification(
                    ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                    fail_notify_msg, ctx.chat_id,
                )

            # 退化通知
            if ctx.quantity_degraded_for_dock:
                remaining = max(ctx.quantity_to_send - len(ctx.delivery_contents), 0)
                await self.handler.send_delivery_failure_notification(
                    ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                    f"⚠️ 对接卡券暂不支持多数量发货：订单数量 {ctx.quantity_to_send} 张，"
                    f"已自动发送 {len(ctx.delivery_contents)} 张，"
                    f"剩余 {remaining} 张请手动补发或改用自有卡券",
                    ctx.chat_id,
                )
            elif ctx.quantity_degraded_for_fixed_content:
                remaining = max(ctx.quantity_to_send - len(ctx.delivery_contents), 0)
                await self.handler.send_delivery_failure_notification(
                    ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                    f"⚠️ 固定内容卡券（{self.handler._last_delivery_card_type} 类型）"
                    f"不支持多数量发货：订单数量 {ctx.quantity_to_send} 张，"
                    f"仅发送 1 张固定内容（剩余 {remaining} 张未发）。"
                    f"如需多数量发送不同卡密，请改用 data 或 api 类型卡券",
                    ctx.chat_id,
                )
            elif len(ctx.delivery_contents) > 1:
                await self.handler.send_delivery_failure_notification(
                    ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                    f"多数量发货成功，共发送 {len(ctx.delivery_contents)} 个卡券",
                    ctx.chat_id,
                )
            else:
                await self.handler.send_delivery_failure_notification(
                    ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                    "发货成功", ctx.chat_id,
                )

            return DeliveryResult.ok()

        except Exception as e:
            logger.error(f"发送通知异常: {self._safe_str(e)}")
            return DeliveryResult.ok()  # 不阻断管道

    # ============================================================
    # 管道后处理
    # ============================================================

    async def _handle_post_pipeline(self, ctx: DeliveryContext) -> None:
        """管道执行完成后的后处理（锁释放、失败通知等）。"""
        # 释放 Redis 锁
        if ctx.redis_lock_acquired and ctx.lock_result:
            try:
                from common.db.redis_client import release_delivery_lock
                released = await release_delivery_lock(ctx.lock_result)
                if released:
                    logger.info(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'Redis分布式锁已释放: {ctx.order_id}'
                    )
                else:
                    logger.warning(
                        f'[{ctx.msg_time}] 【{self.cookie_id}】'
                        f'Redis分布式锁释放失败: {ctx.order_id}'
                    )
            except Exception as e:
                logger.warning(
                    f'[{ctx.msg_time}] 【{self.cookie_id}】'
                    f'Redis分布式锁释放异常: {ctx.order_id}, error={e}'
                )

        logger.info(
            f'[{ctx.msg_time}] 【{self.cookie_id}】'
            f'自动发货处理完成: {ctx.lock_key}'
        )

    # ============================================================
    # 失败回退（供管道外部调用）
    # ============================================================

    async def handle_delivery_failure(
        self, ctx: DeliveryContext, reason: str
    ) -> None:
        """发货失败时的回退处理。"""
        if ctx.skip_confirm_for_card_only:
            logger.info(
                f'[{ctx.msg_time}] 【{self.cookie_id}】'
                f'card_only 模式：保留 pre_check 写入的禁止发货原因，不重复通知买家。'
                f'order_id={ctx.order_id}'
            )
        else:
            await self.handler._update_delivery_fail_reason(ctx.order_id, reason)
            await self.handler.send_delivery_failure_notification(
                ctx.send_user_name, ctx.send_user_id, ctx.item_id,
                reason, ctx.chat_id,
            )
