from __future__ import annotations

"""CompareTool：对比上一轮命中的 2-3 个商品。

触发场景：
    - "这两个有什么区别"
    - "A 和 B 哪个适合干皮"
    - "对比一下第一个和第三个"
    - 主动型："帮我对比下"（默认取 last_hits 前 2 个）

设计意图：
    把对比拆成"定位 + 结构化抽取"两步：
        ① 用 reference 模块把"第一个和第三个"/"华为和小米"映射回具体商品；
        ② 用 agent.comparison.build_comparison 产出干净的维度表 + 购买建议。
    iOS 端拿到的是可直接渲染的 comparison 结构（products + rows + recommendation），
    而不是一段自由发挥的文本。

依赖：
    - ProductStore.get_product_detail：取每个商品的全貌（spec/faq/review）
    - session.recall_hits()：上一轮命中商品，供指代定位
"""

import logging
import re
from typing import Any

from agent.comparison import build_comparison
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.resolve import resolve_many
from agent.tools.reference import resolve_by_name, resolve_indices
from store.product_store import ProductStore


logger = logging.getLogger(__name__)

# Compare exactly two products for a clear two-column table.
_MAX_COMPARE = 2

_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")
_GENERIC_NAME_TOKENS = {
    "pro", "max", "plus", "ultra", "mini", "air", "gb", "tb", "手机", "商品",
    "旗舰", "推荐", "列表", "差异", "区别", "对比", "比较", "两个", "两款",
    "这两个", "这两款", "哪款", "哪个",
}


class CompareTool:
    name: str = "compare"

    def __init__(self, product_store: ProductStore | None = None) -> None:
        self._store = product_store or ProductStore()

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        last_hits = session.recall_hits()
        if len(last_hits) < 2:
            return self._plain(
                "还没有可对比的商品哦，先让我帮你推荐几款，"
                "然后说「对比一下第一个和第二个」就可以。"
            )

        # The comparison screen may provide product IDs directly.
        explicit_ids = slots.get("product_ids")
        if explicit_ids:
            product_ids = [pid for pid in explicit_ids][:_MAX_COMPARE]
        else:
            product_ids = self._resolve_targets(query, last_hits)
            # Use semantic resolution only when rules cannot find two products. Accept
            # it only when both resolve, avoiding an invented second item.
            if len(product_ids) < 2:
                llm_ids = self._resolve_targets_llm(query, last_hits)
                if len(llm_ids) >= 2:
                    product_ids = llm_ids

        if len(product_ids) < 2:
            # Mark a pending comparison before asking, so an ordinal answer returns to
            # this tool. Candidates are the complete last-hit set and need not be copied.
            session.set("pending_compare", {"hit_count": len(last_hits)})
            return self._plain(
                "想对比哪几款呢？可以说「对比第一个和第三个」，"
                "或者「对比华为和小米那两款」。"
            )

        # Clear resolved pending state so it does not capture the next turn.
        session.set("pending_compare", None)

        details = []
        for pid in product_ids:
            detail = self._store.get_product_detail(pid)
            if detail is not None:
                details.append(detail)
        if len(details) < 2:
            return self._plain("这些商品的资料不太全，换两款再试试对比？")

        focus = slots.get("focus") or query
        try:
            comparison = build_comparison(details, focus=focus)
        except Exception:  # noqa: BLE001 - comparison must not fail the whole turn
            logger.exception("build_comparison failed")
            return self._plain("对比生成出了点问题，稍后再试或换两款商品？")

        # Store compared products so later turns can refer to them by position.
        session.set(
            "last_hits",
            [
                {"product_id": p["product_id"], "title": p["title"]}
                for p in comparison["products"]
            ],
        )

        # Provide neutral guidance and let the user judge the objective comparison table.
        titles = "」「".join(p["title"][:14] for p in comparison["products"])
        narrative = f"已为你对比「{titles}」，下面是各维度对照，可按自己看重的方面来选～"

        return ToolResult(
            tool_name=self.name,
            payload={"action": "compare", "comparison": comparison},
            narrative_override=narrative,
            needs_composer=False,
        )

    def _resolve_targets(
        self, query: str, last_hits: list[dict[str, Any]]
    ) -> list[str]:
        """Resolve comparison product IDs, preserving order and uniqueness.

        Explicit names take priority over generic demonstratives, followed by ordinals.
        If no target is stated, use the first two products.
        """
        strict_named = _resolve_context_names(query, last_hits)
        if len(strict_named) >= 2:
            indices = strict_named
        else:
            named = strict_named or resolve_by_name(query, last_hits)
            # With explicit names, remove generic demonstratives before ordinal parsing;
            # otherwise a missing named item could incorrectly use the first two hits.
            index_query = _strip_generic_pair_words(query) if named else query
            indices = resolve_indices(index_query, len(last_hits))
            if len(indices) < 2:
                for i in named:  # Merge matches while preserving order and uniqueness.
                    if i not in indices:
                        indices.append(i)
        if len(indices) < 2 and not (strict_named or resolve_by_name(query, last_hits)):
            indices = list(range(min(2, len(last_hits))))  # No target: use the first two.

        ordered: list[str] = []
        for i in indices[:_MAX_COMPARE]:
            pid = last_hits[i]["product_id"]
            if pid not in ordered:
                ordered.append(pid)
        return ordered

    def _resolve_targets_llm(
        self, query: str, last_hits: list[dict[str, Any]]
    ) -> list[str]:
        """Use semantic resolution when rules cannot identify two products.

        Enrich previous hits with category fields before invoking the shared resolver.
        Return an empty list when unavailable so the caller can clarify.
        """
        enriched: list[dict[str, Any]] = []
        for hit in last_hits:
            item = dict(hit)
            try:
                detail = self._store.get_product_detail(hit["product_id"])
            except Exception:  # noqa: BLE001 - enrichment must not break the main path
                detail = None
            if detail is not None:
                item.setdefault("title", detail.title)
                item["brand"] = detail.brand
                item["category"] = detail.category
                item["sub_category"] = detail.sub_category
            enriched.append(item)

        picked = resolve_many(query, enriched, k=_MAX_COMPARE)
        ordered: list[str] = []
        for i in picked:
            pid = last_hits[i]["product_id"]
            if pid not in ordered:
                ordered.append(pid)
        return ordered

    def _plain(self, text: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"action": "compare", "comparison": None},
            narrative_override=text,
            needs_composer=False,
        )


def _resolve_context_names(query: str, hits: list[dict[str, Any]]) -> list[int]:
    """Match explicitly named brands or models in the previous hits.

    Unlike the recall-oriented generic resolver, this matcher ignores generic model
    terms such as ``Pro`` and favors conservative identification.
    """
    text = query or ""
    lowered = text.lower()
    mentions: list[tuple[int, int, int]] = []  # (query position, -token length, hit index)

    for i, hit in enumerate(hits):
        tokens = _significant_tokens(hit.get("title", ""))
        tokens.extend(_significant_tokens(hit.get("brand", "")))
        seen: set[str] = set()
        for token in tokens:
            key = token.lower() if token.isascii() else token
            if key in seen:
                continue
            seen.add(key)
            pos = lowered.find(key) if token.isascii() else text.find(token)
            if pos >= 0:
                mentions.append((pos, -len(token), i))
                break

    mentions.sort()
    indices: list[int] = []
    for _, _, i in mentions:
        if i not in indices:
            indices.append(i)
    return indices


def _significant_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _NAME_TOKEN_RE.findall(text or ""):
        key = token.lower() if token.isascii() else token
        if key in _GENERIC_NAME_TOKENS:
            continue
        tokens.append(token)
    return tokens


def _strip_generic_pair_words(query: str) -> str:
    text = query or ""
    for word in ("这两款", "这两个", "那两款", "那两个", "两款", "两个", "这俩", "俩", "二者"):
        text = text.replace(word, " ")
    return text


# Signals that release pending comparison state for a new search or category.
_COMPARE_NEW_SEARCH_WORDS: tuple[str, ...] = (
    "推荐", "找", "有哪些", "有什么", "想买", "给我", "来几款", "换个", "换成", "看看别的",
    "加入购物车", "加购", "下单", "结算",
)

# Relationship words connecting two named products.
_COMPARE_LINK_WORDS: tuple[str, ...] = ("和", "跟", "与", "还有", "以及", "对比", "比较", "vs", "VS")


def is_compare_selection_reply(query: str, hit_count: int) -> bool:
    """Return whether the query resembles an answer to a pending comparison.

    Two ordinals or a relationship word count as an answer. New-search signals release
    the pending state.
    """
    text = (query or "").strip()
    if not text:
        return False
    if any(word in text for word in _COMPARE_NEW_SEARCH_WORDS):
        return False
    if len(resolve_indices(text, max(hit_count, 1))) >= 2:
        return True
    if any(word in text for word in _COMPARE_LINK_WORDS):
        return True
    return False

