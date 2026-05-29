from __future__ import annotations

"""RecommendTool：把现成的 SearchService 包装成一个 Tool。

职责（窄而清晰）：
    - 调 SearchService.search() 拿 hits
    - 把 hits 和 parsed query 塞进 working_memory，给后续 refine 用
    - 返回 ToolResult，让 composer 生成自然语言话术

不做的事：
    - 不做 fallback 降级（hits=[] 时如实返回，让 composer 用合适的话术）
    - 不直接生成话术（那是 composer 的事，便于流式 / 个性化注入）
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from search.search_service import SearchService


# 默认 top-k；composer 不需要更多，前端卡片场景 5 已经够。
DEFAULT_TOP_K = 5


class RecommendTool:
    name: str = "recommend"

    def __init__(self, search_service: SearchService | None = None) -> None:
        # 懒加载：首次 run() 时才实例化 SearchService。
        # SearchService 构造会加载 embedding 模型 + 打开 Chroma（~10s），
        # 放在 __init__ 会让 CLI 启动时卡住几十秒无法响应。
        self._service = search_service

    def _get_service(self) -> SearchService:
        if self._service is None:
            from search.search_service import get_search_service
            self._service = get_search_service()
        return self._service

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        top_k = int(slots.get("top_k", DEFAULT_TOP_K))
        result = self._get_service().search(query, top_k_products=top_k)

        # 工作记忆：refine tool 后续会读这俩字段
        session.set("last_parsed_query", result.parsed.to_dict())
        session.set(
            "last_hits",
            [{"product_id": h.product_id, "title": h.title} for h in result.hits],
        )

        # 给前端的精简商品卡片：只保留渲染需要的字段
        products = [
            {
                "product_id": h.product_id,
                "title": h.title,
                "brand": h.brand,
                "category": h.category,
                "sub_category": h.sub_category,
                "price": h.base_price,
            }
            for h in result.hits
        ]

        # 高层摘要：客户端可用来快速判断本轮性质
        summary = {
            "hit_count": len(result.hits),
            "needs_clarification": result.parsed.needs_clarification,
            "category": result.parsed.category,
            "max_price": result.parsed.max_price,
        }

        payload = {
            "query": query,
            "products": products,
            "summary": summary,
            # debug 块给开发者排查用，前端可忽略
            "debug": {
                "parsed": result.parsed.to_dict(),
                "raw_chunk_count": result.raw_chunk_count,
                "filtered_chunk_count": result.filtered_chunk_count,
                "hits_full": [h.to_dict() for h in result.hits],
            },
        }

        # 给 composer 一点 hint，让它对零命中、需澄清做不同应答
        if result.parsed.needs_clarification:
            hint = "检索系统认为 query 过于模糊，请引导用户补充关键信息（品类、预算、用途）。"
        elif not result.hits:
            hint = "本次未命中任何商品，请坦诚告知用户并给出可能放宽的方向建议。"
        else:
            hint = f"已为用户找到 {len(result.hits)} 款商品，请按推荐理由+对比维度的方式简明介绍。"

        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=hint,
        )
