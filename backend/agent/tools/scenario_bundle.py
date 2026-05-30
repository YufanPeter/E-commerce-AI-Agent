from __future__ import annotations

"""ScenarioBundleTool：把场景化需求拆成多类目组合推荐。

例如“下周去三亚度假，帮我搭配一套从防晒到穿搭的方案”不是单一商品检索，
而是一个场景方案：防晒、穿搭、耳机/出行配件等。这里先用可控规则识别常见
场景并编排多次 SearchService.search，再交给 composer 生成方案说明。
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from search.search_service import SearchService


DEFAULT_SECTION_TOP_K = 2


_TRAVEL_SCENES = ("三亚", "海边", "海岛", "度假", "旅行", "旅游", "出游")


class ScenarioBundleTool:
    name: str = "scenario_bundle"

    def __init__(self, search_service: SearchService | None = None) -> None:
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
        sections = _plan_sections(query)
        service = self._get_service()

        payload_sections: list[dict[str, Any]] = []
        products: list[dict[str, Any]] = []
        last_hits: list[dict[str, str]] = []
        seen_product_ids: set[str] = set()

        for section in sections:
            result = service.search(section["query"], top_k_products=DEFAULT_SECTION_TOP_K)
            section_products: list[dict[str, Any]] = []
            for hit in result.hits:
                product = {
                    "product_id": hit.product_id,
                    "title": hit.title,
                    "brand": hit.brand,
                    "category": hit.category,
                    "sub_category": hit.sub_category,
                    "price": hit.base_price,
                    "section": section["label"],
                }
                section_products.append(product)
                if hit.product_id not in seen_product_ids:
                    seen_product_ids.add(hit.product_id)
                    products.append(product)
                    last_hits.append({"product_id": hit.product_id, "title": hit.title})
            payload_sections.append({
                "label": section["label"],
                "query": section["query"],
                "reason": section["reason"],
                "products": section_products,
                "debug": {
                    "parsed": result.parsed.to_dict(),
                    "raw_chunk_count": result.raw_chunk_count,
                    "filtered_chunk_count": result.filtered_chunk_count,
                },
            })

        if last_hits:
            session.remember_search(
                {
                    "original_query": query,
                    "intent": "scenario_bundle",
                    "sections": [s["label"] for s in payload_sections],
                },
                last_hits,
            )

        return ToolResult(
            tool_name=self.name,
            payload={
                "query": query,
                "scenario": _scenario_name(query),
                "sections": payload_sections,
                "products": products,
                "summary": {
                    "section_count": len(payload_sections),
                    "hit_count": len(products),
                },
            },
            composer_hint=(
                "这是场景化组合推荐。请按场景步骤组织回答：先说明整体思路，"
                "再按每个 section 给出 1-2 款商品和理由，最后提醒可继续对比或加购。"
                "禁止编造 payload 之外的商品、优惠或功能。"
            ),
        )


def _plan_sections(query: str) -> list[dict[str, str]]:
    text = query or ""
    if any(token in text for token in _TRAVEL_SCENES):
        return [
            {
                "label": "防晒防护",
                "query": "高倍防晒 防水 清爽 户外 海边",
                "reason": "海边紫外线强，优先准备防晒防护。",
            },
            {
                "label": "轻便穿搭",
                "query": "夏季 轻薄 透气 旅行 穿搭",
                "reason": "旅行场景需要轻薄、透气、易搭配。",
            },
            {
                "label": "出行数码",
                "query": "旅行 便携 降噪 耳机 数码",
                "reason": "路途和休息时适合准备便携数码配件。",
            },
        ]
    return [
        {
            "label": "核心单品",
            "query": text,
            "reason": "先匹配用户场景里的核心需求。",
        },
        {
            "label": "搭配补充",
            "query": f"{text} 搭配 组合",
            "reason": "再补充可一起购买的搭配项。",
        },
    ]


def _scenario_name(query: str) -> str:
    text = query or ""
    if any(token in text for token in _TRAVEL_SCENES):
        return "旅行度假"
    return "场景组合"
