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

import logging
from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.resolve import resolve_one
from agent.tools.reference import resolve_by_name, resolve_indices
from store.product_store import ProductDetail, ProductReview, ProductStore, price_display


logger = logging.getLogger(__name__)


_DEICTIC_WORDS = (
    "这款", "这个", "这件", "这双", "刚才", "刚刚", "上面", "前面", "那个", "那款",
)

_FOCUS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("negative_reviews", ("差评", "缺点", "吐槽", "踩雷", "问题", "不好", "翻车")),
    ("reviews", (
        "评价", "口碑", "评论", "评论区", "大家怎么说", "大家觉得", "用户说",
        "用户反馈", "买家反馈", "买家怎么说", "真实体验", "真实感受", "使用体验",
    )),
    ("sensitive_skin", ("敏感肌", "刺激", "酒精", "过敏", "泛红", "刺痛", "温和")),
    ("size_fit", ("尺码", "偏大", "偏小", "合脚", "版型", "脚感", "码数")),
    ("performance", ("续航", "性能", "屏幕", "拍照", "降噪", "配置", "重量", "轻薄")),
    ("usage", ("怎么用", "使用方法", "注意事项", "保养", "怎么洗", "怎么穿")),
)

_FOCUS_EVIDENCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "reviews": ("user_review", "official_faq", "marketing_description", "product_profile"),
    "negative_reviews": ("user_review", "official_faq", "marketing_description", "product_profile"),
    "sensitive_skin": ("user_review", "official_faq", "marketing_description", "product_profile"),
    "size_fit": ("user_review", "official_faq", "product_profile", "marketing_description"),
    "performance": ("product_profile", "official_faq", "marketing_description", "user_review"),
    "usage": ("official_faq", "marketing_description", "user_review", "product_profile"),
    "general": ("marketing_description", "product_profile", "official_faq", "user_review"),
}


class ProductDetailTool:
    name: str = "product_detail"

    def __init__(
        self,
        product_store: ProductStore | None = None,
        evidence_retriever: Any | None = None,
    ) -> None:
        self._store = product_store or ProductStore()
        self._evidence_retriever = evidence_retriever

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        last_hits = session.recall_hits()
        if not last_hits:
            session.set("pending_detail", None)
            return self._plain(
                query,
                "想详细了解哪款商品呀？先让我推荐几款，然后说「第一个再详细点」就可以。",
            )

        target_id, clarification, focus_query = self._resolve_target_id(query, last_hits, session, slots)
        if not target_id:
            return self._plain(query, clarification or "想了解哪一款呢？可以说「第一个详细说说」。")

        # Clear resolved clarification state so it does not capture the next turn.
        session.set("pending_detail", None)

        detail = self._store.get_product_detail(target_id)
        if detail is None:
            return self._plain(query, "这款商品的详情资料暂时不完整，换一款我再帮你看看？")

        focus = _detect_focus_aspect(focus_query)
        evidence = self._retrieve_evidence(detail, focus_query, focus)
        session.set("last_focus_product_id", detail.product_id)

        payload = {
            "query": focus_query,
            "focus_aspect": focus,
            "product": _product_payload(detail),
            "evidence": evidence,
        }

        if not evidence:
            payload["evidence_note"] = "no_relevant_evidence"

        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=_composer_hint(focus, bool(evidence)),
            needs_composer=True,
        )

    def _resolve_target_id(
        self,
        query: str,
        last_hits: list[dict[str, Any]],
        session: AgentSession,
        slots: dict[str, Any],
    ) -> tuple[str | None, str | None, str]:
        explicit_id = slots.get("product_id")
        if explicit_id:
            return str(explicit_id), None, query

        # First consume pending clarification so ordinals resolve within its candidate
        # subset rather than the complete previous result list.
        pending = session.get("pending_detail")
        if pending and pending.get("candidates"):
            return self._resolve_pending_detail(query, pending, session)

        indices = resolve_indices(query, len(last_hits))
        if indices:
            index = indices[0]
            if 0 <= index < len(last_hits):
                return last_hits[index]["product_id"], None, query

        searchable_hits = self._hits_with_store_context(last_hits)
        name_matches = resolve_by_name(query, searchable_hits)
        if len(name_matches) == 1:
            return last_hits[name_matches[0]]["product_id"], None, query
        if len(name_matches) > 1:
            focus_id = session.get("last_focus_product_id")
            if focus_id:
                matched_ids = {last_hits[i]["product_id"] for i in name_matches}
                if str(focus_id) in matched_ids:
                    return str(focus_id), None, query
            candidates = [
                {
                    "product_id": last_hits[i]["product_id"],
                    "title": searchable_hits[i].get("title", f"第{i + 1}款"),
                    "brand": searchable_hits[i].get("brand", ""),
                    "category": searchable_hits[i].get("category", ""),
                    "sub_category": searchable_hits[i].get("sub_category", ""),
                }
                for i in name_matches[:3]
            ]
            # Resolve by ordinal, unique name, then semantic matching over catalog fields.
            picked = resolve_one(query, candidates)
            if picked is not None:
                return str(candidates[picked]["product_id"]), None, query
            # Retain the clarification subset and requested focus for the next turn.
            session.set("pending_detail", {"candidates": candidates, "focus_query": query})
            titles = "、".join(str(c["title"])[:16] for c in candidates)
            return None, f"你提到的商品有点多，是想了解「{titles}」里的哪一款？", query

        focus_id = session.get("last_focus_product_id")
        if _is_attribute_followup(query) and focus_id and any(hit["product_id"] == focus_id for hit in last_hits):
            return str(focus_id), None, query

        if _has_deictic(query) and focus_id and any(hit["product_id"] == focus_id for hit in last_hits):
            return str(focus_id), None, query

        if _has_deictic(query) or len(last_hits) == 1:
            return last_hits[0]["product_id"], None, query

        return None, "想了解哪一款呢？可以说「第一个评价怎么样」或「小米那款详细说说」。", query

    def _resolve_pending_detail(
        self,
        query: str,
        pending: dict[str, Any],
        session: AgentSession,
    ) -> tuple[str | None, str | None, str]:
        """Consume a pending clarification as either a selection or a topic change."""
        candidates = pending.get("candidates") or []
        focus_query = f"{pending.get('focus_query', '')} {query}".strip()

        # Look for a unique match in the candidate subset, preferring ordinals.
        resolved = _pick_from_candidates(query, candidates)
        if resolved is not None:
            return resolved, None, focus_query

        # Temporarily clear pending state and try normal resolution for a topic change.
        candidate_ids = {str(c["product_id"]) for c in candidates}
        last_hits = session.recall_hits()
        session.set("pending_detail", None)
        target_id, clarification, fallback_query = self._resolve_target_id(
            query, last_hits, session, {}
        )
        if target_id and str(target_id) not in candidate_ids:
            # A clear match outside the subset confirms a topic change.
            return target_id, clarification, fallback_query

        # Restore pending state when the answer still needs clarification.
        session.set("pending_detail", pending)
        titles = "、".join(str(c.get("title", ""))[:16] for c in candidates[:3])
        return None, f"想了解「{titles}」里的哪一款呢？说「第一款」或直接报品牌名都行。", query

    def _hits_with_store_context(self, last_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for hit in last_hits:
            item = dict(hit)
            try:
                detail = self._store.get_product_detail(hit["product_id"])
            except Exception:  # noqa: BLE001 - enrichment must not break title matching
                detail = None
            if detail is not None:
                item.setdefault("title", detail.title)
                item["brand"] = detail.brand
                # Include category fields so semantic disambiguation has enough context.
                item["category"] = detail.category
                item["sub_category"] = detail.sub_category
            enriched.append(item)
        return enriched

    def _retrieve_evidence(
        self,
        detail: ProductDetail,
        query: str,
        focus: str,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        evidence = self._retrieve_rag_evidence(detail, query, focus, limit=limit)
        if not evidence:
            evidence = _fallback_evidence(detail, focus, limit=limit)
        return _prioritize_evidence(evidence, focus)[:limit]

    def _retrieve_rag_evidence(
        self,
        detail: ProductDetail,
        query: str,
        focus: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            retriever = self._evidence_retriever
            if retriever is None:
                from search.search_service import get_search_service

                retriever = get_search_service()._retriever  # noqa: SLF001 - reuse warmed retriever
            chunks = retriever.search(
                query=f"{detail.title} {query} {focus}",
                top_k=max(limit * 2, 8),
                where={"product_id": detail.product_id},
            )
        except Exception as exc:  # noqa: BLE001 - RAG failures fall back to ProductStore
            logger.warning("product_detail RAG evidence failed, fallback to store: %r", exc)
            return []

        evidence: list[dict[str, Any]] = []
        for chunk in chunks:
            source_type = str(chunk.metadata.get("chunk_type", chunk.chunk_type))
            if focus == "negative_reviews" and source_type == "user_review":
                polarity = str(chunk.metadata.get("polarity", ""))
                if polarity and polarity != "negative":
                    continue
            evidence.append(
                {
                    "source_type": source_type,
                    "title": str(chunk.metadata.get("title") or _source_title(source_type)),
                    "text": chunk.document,
                    "score": _score_from_distance(chunk.distance),
                    "metadata": dict(chunk.metadata),
                }
            )
        return _focus_filter_evidence(evidence, focus, limit)

    def _plain(self, query: str, text: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"query": query, "product": None, "evidence": []},
            narrative_override=text,
            needs_composer=False,
        )


def _detect_focus_aspect(query: str) -> str:
    text = query or ""
    for focus, keywords in _FOCUS_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return focus
    return "general"


def _has_deictic(query: str) -> bool:
    return any(word in (query or "") for word in _DEICTIC_WORDS)


# Signals that release pending detail state for a new search or category.
_NEW_SEARCH_WORDS: tuple[str, ...] = (
    "推荐", "找", "有哪些", "有什么", "想买", "给我", "来几款", "换个", "换成", "看看别的",
)

# Comparison intent also releases pending detail state.
_COMPARE_INTENT_WORDS: tuple[str, ...] = (
    "对比", "比较", "区别", "哪个好", "哪款好", "哪个更", "哪款更", "差异",
)


def _pick_from_candidates(query: str, candidates: list[dict[str, Any]]) -> str | None:
    """Map a clarification answer to one product in the candidate subset.

    The shared resolver uses ordinal, unique-name, then semantic LLM matching. Return
    ``None`` when unresolved so the caller can ask again.
    """
    picked = resolve_one(query, candidates)
    return str(candidates[picked]["product_id"]) if picked is not None else None


def is_detail_selection_reply(query: str) -> bool:
    """Return whether the query resembles an answer to a pending detail question.

    Ordinals, demonstratives, and short names count as answers. New-search or comparison
    signals release the pending state instead.
    """
    text = (query or "").strip()
    if not text:
        return False
    if any(word in text for word in _NEW_SEARCH_WORDS):
        return False
    if any(word in text for word in _COMPARE_INTENT_WORDS):
        return False
    if resolve_indices(text, 99):
        return True
    if _has_deictic(text):
        return True
    return len(text) <= 8



def _is_attribute_followup(query: str) -> bool:
    text = query or ""
    has_attribute = any(keyword in text for _, keywords in _FOCUS_KEYWORDS for keyword in keywords)
    has_question = any(word in text for word in ("怎么样", "如何", "好吗", "好不好", "有没有", "表现"))
    is_new_search = any(word in text for word in ("推荐", "找", "有哪些", "有什么", "想买", "给我"))
    return has_attribute and has_question and not is_new_search


def _product_payload(detail: ProductDetail) -> dict[str, Any]:
    return {
        "product_id": detail.product_id,
        "title": detail.title,
        "brand": detail.brand,
        "category": detail.category,
        "sub_category": detail.sub_category,
        "price_display": price_display(detail.price_range),
    }


def _fallback_evidence(detail: ProductDetail, focus: str, limit: int) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []

    if detail.marketing_description:
        evidence.append(
            {
                "source_type": "marketing_description",
                "title": "商品描述",
                "text": detail.marketing_description,
                "score": None,
                "metadata": {"product_id": detail.product_id},
            }
        )

    sku_summary = _sku_summary(detail)
    if sku_summary:
        evidence.append(
            {
                "source_type": "product_profile",
                "title": "规格概况",
                "text": sku_summary,
                "score": None,
                "metadata": {"product_id": detail.product_id},
            }
        )

    for faq in detail.faqs[:3]:
        evidence.append(
            {
                "source_type": "official_faq",
                "title": faq.question,
                "text": faq.answer,
                "score": None,
                "metadata": {"product_id": detail.product_id, "source_index": faq.source_index},
            }
        )

    reviews = _reviews_for_focus(detail.reviews, focus)
    for review in reviews[:4]:
        evidence.append(_review_evidence(detail.product_id, review))

    return _prioritize_evidence(evidence, focus)[:limit]


def _reviews_for_focus(reviews: list[ProductReview], focus: str) -> list[ProductReview]:
    if focus == "negative_reviews":
        negative = [review for review in reviews if review.polarity == "negative" or review.rating <= 2]
        return negative or sorted(reviews, key=lambda review: review.rating)
    if focus in {"reviews", "sensitive_skin", "size_fit"}:
        return reviews
    return reviews[:2]


def _review_evidence(product_id: str, review: ProductReview) -> dict[str, Any]:
    return {
        "source_type": "user_review",
        "title": f"{review.nickname} · {review.rating} 星",
        "text": review.content,
        "score": None,
        "metadata": {
            "product_id": product_id,
            "source_index": review.source_index,
            "rating": review.rating,
            "polarity": review.polarity,
        },
    }


def _prioritize_evidence(evidence: list[dict[str, Any]], focus: str) -> list[dict[str, Any]]:
    priority = _FOCUS_EVIDENCE_PRIORITY.get(focus, _FOCUS_EVIDENCE_PRIORITY["general"])
    rank = {source_type: index for index, source_type in enumerate(priority)}
    return sorted(
        evidence,
        key=lambda item: (
            rank.get(str(item.get("source_type")), len(rank)),
            -(float(item.get("score") or 0)),
        ),
    )


def _focus_filter_evidence(evidence: list[dict[str, Any]], focus: str, limit: int) -> list[dict[str, Any]]:
    prioritized = _prioritize_evidence(evidence, focus)
    preferred_types = set(_FOCUS_EVIDENCE_PRIORITY.get(focus, ())[:2])
    preferred = [item for item in prioritized if item.get("source_type") in preferred_types]
    if len(preferred) >= limit:
        return preferred[:limit]
    seen = {id(item) for item in preferred}
    rest = [item for item in prioritized if id(item) not in seen]
    return (preferred + rest)[:limit]


def _sku_summary(detail: ProductDetail) -> str:
    if not detail.skus:
        return ""
    specs = []
    for sku in detail.skus[:4]:
        if sku.properties:
            specs.append("、".join(f"{key}:{value}" for key, value in sku.properties.items()))
    joined = "；".join(specs)
    return f"共有 {len(detail.skus)} 个在售规格。{joined}" if joined else f"共有 {len(detail.skus)} 个在售规格。"


def _score_from_distance(distance: float | None) -> float | None:
    if distance is None:
        return None
    return round(1 / (1 + float(distance)), 4)


def _source_title(source_type: str) -> str:
    return {
        "user_review": "用户评价",
        "official_faq": "官方问答",
        "marketing_description": "商品描述",
        "product_profile": "规格概况",
    }.get(source_type, source_type or "商品证据")


def _composer_hint(focus: str, has_evidence: bool) -> str:
    if focus in {"general", "performance"}:
        evidence_rule = "证据不足时要说目前资料有限，不要编造。" if not has_evidence else "必须基于 payload.evidence 回答，不要编造。"
        focus_rule = (
            "用户关心性能/配置/屏幕/续航等，请优先引用规格和 FAQ。"
            if focus == "performance"
            else "用户想了解这款整体怎么样，请给出适合快速决策的短结论。"
        )
        return (
            "这是 product_detail 单品介绍问答。请只输出 1 个自然段，"
            "不要输出 JSON，不要分点，不要换行，不要 emoji。"
            "控制在 90 个中文字符以内，包含产品定位、1-2 个核心卖点，"
            "以及适合人群或购买前注意点；禁止写成长段参数介绍。"
            "注意：这是了解产品本身，不要写成用户评价总结。"
            f"{focus_rule}{evidence_rule}"
        )

    if focus in {"reviews", "negative_reviews"}:
        evidence_rule = "证据不足时要说目前资料有限，不要编造。" if not has_evidence else "必须基于 payload.evidence 回答，不要编造。"
        focus_rule = (
            "用户想了解差评或缺点，请在缺点段优先总结负面证据；如果负面证据不足，要明确说明。"
            if focus == "negative_reviews"
            else "用户想了解真实评价和口碑，请优先总结 review 证据里的共性，兼顾正负两面。"
        )
        return (
            "这是 product_detail 单品评价问答。请只输出 3 个独立段落，每段单独一行，"
            "不要输出 JSON，不要额外加开场或结尾。每段写 1-2 句，"
            "每段不超过 70 个中文字符；优点和缺点各总结 2-3 个核心点，不要写成大段流水账。固定格式为："
            "✨ 总结：...\n🌟 优点：...\n🔍 缺点：...\n"
            "注意：这是看用户评价/口碑，不要写成商品参数介绍。"
            f"{focus_rule}{evidence_rule}"
        )

    focus_text = {
        "reviews": "用户想了解真实评价和口碑，请优先总结 review 证据里的共性。",
        "negative_reviews": "用户想了解差评或缺点，请优先说负面证据；如果负面证据不足，要明确说明。",
        "sensitive_skin": "用户关心敏感肌/刺激风险，请只基于 evidence 判断，不要做医疗承诺。",
        "size_fit": "用户关心尺码/版型/脚感，请优先引用评价和 FAQ。",
        "usage": "用户关心使用方法或注意事项，请优先引用 FAQ 和商品描述。",
    }[focus]
    evidence_rule = "证据不足时要说目前资料有限，不要编造。" if not has_evidence else "必须基于 payload.evidence 回答，不要编造。"
    return (
        "这是 product_detail 单品深挖问答。请用 3-5 句中文自然回答，不要输出 JSON。"
        f"{focus_text}{evidence_rule}"
    )
