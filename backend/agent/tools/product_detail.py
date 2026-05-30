from __future__ import annotations

"""ProductDetailTool：单品深挖（"第二个详细说说"/"这款敏感肌能用吗"）。

触发场景：
    - "第二个详细介绍下"
    - "第 1 款的成分能说说吗"
    - "这款适合敏感肌吗"

实现流程：
    ① 从 working_memory 取 last_hits（上一轮推荐的商品引用）。
    ② 指代消解：把"第二个/这款/那个珀莱雅的"定位到一个 product_id
       （reference.resolve_indices + resolve_by_title；都没命中默认第一个）。
    ③ ProductStore.get_product_detail(pid) 拉商品全貌（卖点 + FAQ + 评价 + SKU）。
    ④ 把 query 当作 focus_aspect 透传，composer 用 product_detail 系统提示按
       "深度介绍"口吻作答（语气适配已在 composer._TOOL_SYSTEM_PROMPT 里就绪）。

与 CompareTool 共享 ProductStore 这个确定性事实来源。
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.reference import resolve_by_title, resolve_indices


# 给 composer 的评价/FAQ 取样上限，避免 token 爆炸。
_MAX_FAQS = 3
_MAX_REVIEWS = 4


class ProductDetailTool:
    name: str = "product_detail"

    def __init__(self, product_store: Any | None = None) -> None:
        # 懒加载 ProductStore：与 RecommendTool 一致，避免 import/构造期 IO。
        self._store = product_store

    def _get_store(self):
        if self._store is None:
            from store.product_store import DEFAULT_DB_PATH, ProductStore
            self._store = ProductStore(DEFAULT_DB_PATH)
        return self._store

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        last_hits = session.recall_hits()
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

        idx = self._resolve_target_index(query, last_hits)
        product_id = last_hits[idx]["product_id"]

        detail = self._get_store().get_product_detail(product_id)
        if detail is None:
            # 记忆里有引用但库里查不到（数据漂移）：坦诚兜底，不编造。
            return ToolResult(
                tool_name=self.name,
                payload={"query": query, "product": None, "product_id": product_id},
                narrative_override="这款商品信息暂时查不到了，要不换一款看看？",
                needs_composer=False,
            )

        # 命中的商品同时写回工作记忆，让接下来的"加入购物车/再便宜点"有锚点。
        session.set("last_focus_product_id", product_id)

        payload = self._build_payload(query, detail, idx)
        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=(
                f"用户关注点：{query}。请围绕该关注点，结合卖点与真实评价深入介绍这款商品。"
            ),
        )

    # ------------------------------ 内部 ------------------------------

    def _resolve_target_index(self, query: str, last_hits: list[dict]) -> int:
        """把用户的指代定位到 last_hits 的下标；都识别不了时默认第一个。"""
        indices = resolve_indices(query, len(last_hits))
        if indices:
            return indices[0]
        by_title = resolve_by_title(query, last_hits)
        if by_title is not None:
            return by_title
        return 0

    def _build_payload(self, query: str, detail: Any, idx: int) -> dict[str, Any]:
        faqs = [
            {"question": f.question, "answer": f.answer}
            for f in detail.faqs[:_MAX_FAQS]
        ]
        reviews = [
            {"rating": r.rating, "polarity": r.polarity, "content": r.content}
            for r in detail.reviews[:_MAX_REVIEWS]
        ]
        return {
            "query": query,
            "focus_aspect": query,
            "selected_index": idx,
            # composer._trim_payload_for_llm 认 hits[]，复用同一裁剪路径。
            "hits": [
                {
                    "title": detail.title,
                    "brand": detail.brand,
                    "category": detail.category,
                    "sub_category": detail.sub_category,
                    "base_price": detail.price_range.min_price,
                }
            ],
            "product": {
                "product_id": detail.product_id,
                "title": detail.title,
                "brand": detail.brand,
                "price_display": _price_display(detail),
                "marketing_description": detail.marketing_description,
                "faqs": faqs,
                "reviews": reviews,
            },
        }


def _price_display(detail: Any) -> str:
    pr = detail.price_range
    if pr.min_price == pr.max_price:
        return f"¥{pr.min_price:g}"
    return f"¥{pr.min_price:g} 起"
