from __future__ import annotations

"""ProductDetailTool：单品深挖（"第二个详细说说"/"这款敏感肌能用吗"）。

触发场景：
    - "第二个详细介绍下"
    - "第 1 款的成分能说说吗"
    - "这款适合敏感肌吗"

设计意图：
    RecommendTool 出的卡片只有 title/brand/price 几个字段，
    用户想深挖时（成分/评价/使用方法/适用人群）需要把该商品所有
    chunks（marketing_description + product_profile + official_faq + user_review）
    聚合成"商品全貌"再让 composer 答。
    
    与 CompareTool 共享 ProductService 这个底层 capability。

依赖：
    - ProductService.get_full(product_id)
    - session.last_hits 用于把"第二个"映射到 product_id
    - query 里也支持用 title 关键字定位（"那个珀莱雅的"）

TODO（待实现）：
    1. _resolve_target_id(query, last_hits) → str | None
       - 正则匹配序号
       - 关键字匹配 last_hits.title
       - 都没命中返回 None → narrative_override 让用户澄清
    2. ProductService.get_full(pid) 拿全貌
    3. 按用户具体问点（成分/适用/使用方法）裁剪 chunks，避免给 composer 太多 token
       - 简单方案：把 query 当作"focus_aspect"传给 composer，让它聚焦
    4. composer 适配：tool_name=='product_detail' 时按"深度介绍"模式说话
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult


class ProductDetailTool:
    name: str = "product_detail"

    def __init__(self, product_service: Any | None = None) -> None:
        # TODO: 类型改成 ProductService（Step 1 后落地）
        self._product_service = product_service

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        # TODO: 真正实现
        last_hits = session.get("last_hits") or []
        if not last_hits:
            return ToolResult(
                tool_name=self.name,
                payload={"query": query, "product": None},
                narrative_override=(
                    "想详细了解哪款商品呀？先让我推荐几款，"
                    "然后说「第一个再详细点」就可以～"
                ),
                needs_composer=False,
            )
        return ToolResult(
            tool_name=self.name,
            payload={
                "query": query,
                "product": last_hits[0],  # 真实实现后按 query 精确定位
            },
            narrative_override="商品详情深挖功能即将上线～",
            needs_composer=False,
        )
