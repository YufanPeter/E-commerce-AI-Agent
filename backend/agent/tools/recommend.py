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

_ACTIVE_CLARIFY_SUB_CATEGORIES: dict[str, tuple[str, ...]] = {
    "智能手机": ("拍照", "续航", "性价比"),
    "笔记本电脑": ("轻薄便携", "性能", "续航"),
    "真无线耳机": ("降噪", "音质", "佩戴舒适"),
    "跑鞋": ("缓震", "轻量", "竞速"),
}

_ACTIVE_CLARIFY_CATEGORIES: dict[str, tuple[str, ...]] = {
    "美妆护肤": ("肤质", "功效", "预算"),
    "数码电子": ("使用场景", "品牌偏好", "预算"),
    "服饰运动": ("穿着场景", "尺码/风格", "预算"),
    "食品生活": ("使用场景", "口味/材质偏好", "预算"),
}


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
        # base_parsed 由 RefineTool 注入：表示这是一次"细化"，需把本轮解析叠加到
        # 上一轮结构化意图上（见 SearchService.search / ParsedQuery.merge_base）。
        base = slots.get("base_parsed")
        result = self._get_service().search(query, top_k_products=top_k, base=base)

        if base is None:
            active_clarify = _active_clarification_text(result.parsed)
            if active_clarify:
                return ToolResult(
                    tool_name=self.name,
                    payload={
                        "query": query,
                        "products": [],
                        "summary": {
                            "hit_count": 0,
                            "needs_clarification": True,
                            "category": getattr(result.parsed, "category", None),
                            "max_price": getattr(result.parsed, "max_price", None),
                        },
                        "debug": {
                            "parsed": result.parsed.to_dict(),
                            "active_clarification": True,
                        },
                    },
                    narrative_override=active_clarify,
                    needs_composer=False,
                )

        # 工作记忆（WorkingMemory 契约）：refine/compare/detail 后续会读这俩字段。
        # 存的是【结构化】ParsedQuery（dict），下一轮才能做无损约束叠加。
        session.remember_search(
            result.parsed.to_dict(),
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


def _active_clarification_text(parsed: Any) -> str | None:
    """对信息不足但方向明确的购物请求主动追问，避免盲推。"""
    if getattr(parsed, "needs_clarification", False):
        return None
    if _has_decision_signal(parsed):
        return None

    sub_category = getattr(parsed, "sub_category", None)
    category = getattr(parsed, "category", None)

    if sub_category in _ACTIVE_CLARIFY_SUB_CATEGORIES:
        options = _ACTIVE_CLARIFY_SUB_CATEGORIES[sub_category]
        return (
            f"可以，我先帮你缩小范围：你选 {sub_category} 更看重"
            f"{_join_options(options)}？预算大概多少？"
        )
    if category in _ACTIVE_CLARIFY_CATEGORIES and not sub_category:
        options = _ACTIVE_CLARIFY_CATEGORIES[category]
        return (
            f"可以，我先确认下方向：这次更看重{_join_options(options)}？"
            "补充一点后我再给你推荐更准。"
        )
    return None


def _has_decision_signal(parsed: Any) -> bool:
    return any([
        getattr(parsed, "max_price", None) is not None,
        getattr(parsed, "min_price", None) is not None,
        bool(getattr(parsed, "brand_include", None)),
        bool(getattr(parsed, "brand_exclude", None)),
        bool(getattr(parsed, "negative_ingredients", None)),
        bool(getattr(parsed, "soft_terms", None)),
    ])


def _join_options(options: tuple[str, ...]) -> str:
    if len(options) <= 1:
        return options[0] if options else ""
    return "、".join(options[:-1]) + f"还是{options[-1]}"
