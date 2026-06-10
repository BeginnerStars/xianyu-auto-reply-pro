"""
飞书交互卡片消息构建器

提供飞书卡片消息 v2 schema 的构建功能，用于发送富文本交互式通知。

功能：
1. 发货成功卡片通知
2. 发货失败卡片通知
3. 卡券库存预警卡片通知
4. 订单汇总卡片通知

卡片使用原生表格组件展示结构化数据，支持边框样式。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


class FeishuCardBuilder:
    """飞书交互卡片消息构建器 (Schema 2.0)"""

    # ==================== 公共样式常量 ====================

    _HEADER_TEMPLATE_BLUE = "blue"
    _HEADER_TEMPLATE_RED = "red"
    _HEADER_TEMPLATE_ORANGE = "orange"
    _HEADER_TEMPLATE_GREEN = "green"
    _HEADER_TEMPLATE_PURPLE = "purple"

    # ==================== 卡片构建方法 ====================

    @staticmethod
    def build_delivery_success_card(
        order_id: str,
        item_name: str,
        card_content: str,
        amount: Optional[float] = None,
    ) -> Dict[str, Any]:
        """构建发货成功交互卡片

        Args:
            order_id: 订单号
            item_name: 商品名称
            card_content: 卡券内容
            amount: 订单金额（可选）

        Returns:
            飞书卡片 JSON 结构 (schema 2.0)
        """
        # 构建表格行
        table_rows = [
            [{"text": "订单号", "style": ["bold"]}, {"text": order_id}],
            [{"text": "商品名称", "style": ["bold"]}, {"text": item_name}],
            [{"text": "卡券内容", "style": ["bold"]}, {"text": card_content}],
        ]
        if amount is not None:
            table_rows.append(
                [{"text": "订单金额", "style": ["bold"]}, {"text": f"¥{amount:.2f}"}]
            )
        table_rows.append(
            [{"text": "发货时间", "style": ["bold"]}, {"text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}]
        )

        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "✅ 发货成功"},
                "template": FeishuCardBuilder._HEADER_TEMPLATE_GREEN,
            },
            "body": {
                "elements": [
                    {
                        "tag": "table",
                        "page_size": 20,
                        "row_height": "low",
                        "header_style": {
                            "text_align": "left",
                            "text_size": "normal",
                            "background_style": "grey",
                            "text_color": "grey",
                            "bold": True,
                        },
                        "columns": [
                            {"name": "field", "display_name": "字段", "data_type": "text", "width": "auto"},
                            {"name": "value", "display_name": "内容", "data_type": "text", "width": "auto"},
                        ],
                        "rows": [
                            {"field": row[0]["text"], "value": row[1]["text"]}
                            for row in table_rows
                        ],
                    }
                ]
            },
        }

    @staticmethod
    def build_delivery_failure_card(
        order_id: str,
        item_name: str,
        error_msg: str,
    ) -> Dict[str, Any]:
        """构建发货失败交互卡片

        Args:
            order_id: 订单号
            item_name: 商品名称
            error_msg: 错误信息

        Returns:
            飞书卡片 JSON 结构 (schema 2.0)
        """
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❌ 发货失败"},
                "template": FeishuCardBuilder._HEADER_TEMPLATE_RED,
            },
            "body": {
                "elements": [
                    {
                        "tag": "table",
                        "page_size": 20,
                        "row_height": "low",
                        "header_style": {
                            "text_align": "left",
                            "text_size": "normal",
                            "background_style": "grey",
                            "text_color": "grey",
                            "bold": True,
                        },
                        "columns": [
                            {"name": "field", "display_name": "字段", "data_type": "text", "width": "auto"},
                            {"name": "value", "display_name": "内容", "data_type": "text", "width": "auto"},
                        ],
                        "rows": [
                            {"field": "订单号", "value": order_id},
                            {"field": "商品名称", "value": item_name},
                            {"field": "错误信息", "value": error_msg},
                            {"field": "失败时间", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                        ],
                    },
                    {
                        "tag": "markdown",
                        "content": "**⚠️ 请尽快检查并手动处理该订单**",
                    },
                ]
            },
        }

    @staticmethod
    def build_inventory_alert_card(
        card_name: str,
        remaining_count: int,
        threshold: int,
    ) -> Dict[str, Any]:
        """构建库存预警交互卡片

        Args:
            card_name: 卡券名称
            remaining_count: 剩余数量
            threshold: 预警阈值

        Returns:
            飞书卡片 JSON 结构 (schema 2.0)
        """
        severity = "🔴 严重不足" if remaining_count == 0 else "🟡 即将耗尽"

        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📦 卡券库存预警"},
                "template": FeishuCardBuilder._HEADER_TEMPLATE_ORANGE,
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"**库存状态：{severity}**",
                    },
                    {
                        "tag": "table",
                        "page_size": 20,
                        "row_height": "low",
                        "header_style": {
                            "text_align": "left",
                            "text_size": "normal",
                            "background_style": "grey",
                            "text_color": "grey",
                            "bold": True,
                        },
                        "columns": [
                            {"name": "field", "display_name": "项目", "data_type": "text", "width": "auto"},
                            {"name": "value", "display_name": "详情", "data_type": "text", "width": "auto"},
                        ],
                        "rows": [
                            {"field": "卡券名称", "value": card_name},
                            {"field": "剩余数量", "value": str(remaining_count)},
                            {"field": "预警阈值", "value": str(threshold)},
                            {"field": "检查时间", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                        ],
                    },
                    {
                        "tag": "markdown",
                        "content": "**💡 请及时补充卡券库存，避免影响自动发货**",
                    },
                ]
            },
        }

    @staticmethod
    def build_order_summary_card(orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构建订单汇总交互卡片

        Args:
            orders: 订单列表，每个订单字典应包含:
                - order_id: 订单号
                - item_name: 商品名称
                - status: 状态（如 'success', 'failed', 'pending'）
                - amount: 金额（可选）

        Returns:
            飞书卡片 JSON 结构 (schema 2.0)
        """
        status_map = {
            "success": "✅ 成功",
            "failed": "❌ 失败",
            "pending": "⏳ 待处理",
        }

        total = len(orders)
        success_count = sum(1 for o in orders if o.get("status") == "success")
        failed_count = sum(1 for o in orders if o.get("status") == "failed")
        pending_count = sum(1 for o in orders if o.get("status") == "pending")

        rows = []
        for order in orders:
            rows.append({
                "order_id": str(order.get("order_id", "")),
                "item_name": str(order.get("item_name", "")),
                "status": status_map.get(order.get("status", ""), order.get("status", "")),
                "amount": f"¥{order['amount']:.2f}" if order.get("amount") is not None else "-",
            })

        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 订单汇总（共 {total} 笔）"},
                "template": FeishuCardBuilder._HEADER_TEMPLATE_BLUE,
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**✅ 成功：{success_count}** | "
                            f"**❌ 失败：{failed_count}** | "
                            f"**⏳ 待处理：{pending_count}**"
                        ),
                    },
                    {
                        "tag": "table",
                        "page_size": 50,
                        "row_height": "low",
                        "header_style": {
                            "text_align": "left",
                            "text_size": "normal",
                            "background_style": "grey",
                            "text_color": "grey",
                            "bold": True,
                        },
                        "columns": [
                            {"name": "order_id", "display_name": "订单号", "data_type": "text", "width": "auto"},
                            {"name": "item_name", "display_name": "商品名称", "data_type": "text", "width": "auto"},
                            {"name": "status", "display_name": "状态", "data_type": "text", "width": "auto"},
                            {"name": "amount", "display_name": "金额", "data_type": "text", "width": "auto"},
                        ],
                        "rows": rows,
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"汇总时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                            }
                        ],
                    },
                ]
            },
        }
