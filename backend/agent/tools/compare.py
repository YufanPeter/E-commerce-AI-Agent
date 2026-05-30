from __future__ import annotations

"""CompareTool：对比上一轮命中的 2-3 个商品。

触发场景：
    - "这两个有什么区别"
    - "A 和 B 哪个适合干皮"
    - "对比一下第一个和第三个"
    - 主动型："帮我对比下"（默认取 last_hits 前 2 个）

实现流程：
    ① 从 working_memory 取 last_hits。不足 2 个 → 引导用户先推荐。
    ② 指代消解定位要对比的下标（reference.resolve_indices；无明确指代默认前两个）。
    ③ ProductStore.get_product_detail 拉每款全貌，抽出公共维度（价格/品牌/品类/
       评分概览）构成结构化 dimensions[]，前端可直接渲染对比表。
    ④ composer 用 compare 系统提示给"什么人选哪个"的决策建议。

设计取舍：
    维度抽取这里只做**确定性公共维度**（价格/品牌/品类/好评率），不调 LLM 抽
    "动态差异维度"——确定性维度足够支撑对比表 + 决策话术，且零额外延迟/幻觉。
    若日后要更丰富的差异维度，可在此加一段 function-calling 抽取（见 git 历史 TODO）。
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.reference import resolve_indices


# 一次最多对比几款：再多对比表会过宽，决策价值也下降。
_MAX_COMPARE = 3


class CompareTool:
    name: str = "compare"

    def __init__(self, product_store: Any | None = None) -> None:
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
        if len(last_hits) < 2:
            return ToolResult(
                tool_name=self.name,
                payload={"query": query, "products": [], "dimensions": []},
                narrative_override=(
                    "还没有可对比的商品哦，先让我帮你推荐几款，"
                    "然后说「对比一下第一个和第二个」就可以。"
                ),
                needs_composer=False,
            )

        indices = self._resolve_targets(query, len(last_hits))
        product_ids = [last_hits[i]["product_id"] for i in indices]

        # 用 get_product_detail 而非 get_products_by_ids：对比要好评概览，
        # 需要 reviews 字段（ProductCandidate 没有，ProductDetail 才有）。
        store = self._get_store()
        details = [d for d in (store.get_product_detail(pid) for pid in product_ids) if d]
        if len(details) < 2:
            return ToolResult(
                tool_name=self.name,
                payload={"query": query, "products": [], "dimensions": []},
                narrative_override="要对比的商品信息暂时查不全，换两款再试试？",
                needs_composer=False,
            )

        products = [self._product_brief(d) for d in details]
        dimensions = self._build_dimensions(details)

        return ToolResult(
            tool_name=self.name,
            payload={
                "query": query,
                "products": products,
                "dimensions": dimensions,
            },
            composer_hint=(
                "请基于这几款商品的对比维度，逐维度说明差异，"
                "最后给出'更看重 X 选哪款、更看重 Y 选哪款'的决策建议。"
            ),
        )

    # ------------------------------ 内部 ------------------------------

    def _resolve_targets(self, query: str, hit_count: int) -> list[int]:
        """定位要对比的下标。无明确指代时默认前两个；超过上限截断。"""
        indices = resolve_indices(query, hit_count)
        if len(indices) < 2:
            # "对比一下" 这种没点名的，默认前两个
            indices = list(range(min(2, hit_count)))
        return indices[:_MAX_COMPARE]

    def _product_brief(self, detail: Any) -> dict[str, Any]:
        return {
            "product_id": detail.product_id,
            "title": detail.title,
            "brand": detail.brand,
            "price": detail.price_range.min_price,
        }

    def _build_dimensions(self, details: list[Any]) -> list[dict[str, Any]]:
        """构造逐维度的横向对比表。每个维度给出每款商品的取值。"""
        def row(label: str, values: list[str]) -> dict[str, Any]:
            return {"label": label, "values": values}

        dims = [
            row("价格", [_price_display(d) for d in details]),
            row("品牌", [d.brand or "—" for d in details]),
            row("品类", [d.sub_category or d.category or "—" for d in details]),
            row("好评概览", [_review_summary(d) for d in details]),
        ]
        return dims


def _price_display(detail: Any) -> str:
    pr = detail.price_range
    if pr.min_price == pr.max_price:
        return f"¥{pr.min_price:g}"
    return f"¥{pr.min_price:g} 起"


def _review_summary(detail: Any) -> str:
    reviews = detail.reviews or []
    if not reviews:
        return "暂无评价"
    positive = sum(1 for r in reviews if r.polarity == "positive")
    avg = sum(r.rating for r in reviews) / len(reviews)
    return f"{avg:.1f}分 / 好评{positive}条（共{len(reviews)}条）"
