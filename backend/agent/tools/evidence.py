from __future__ import annotations

"""从检索命中的 chunk 里挑出能支撑推荐理由的事实。

推荐解说如果只有标题可看，模型就只能写「性能够用又不贵」这种放哪款都成立的话。
这里把和用户需求最相关的原文句子交给 composer，让每句解说都有出处。

- points：卖点依据，来自商品描述、官方问答、非差评的用户评价。
- risk：一条和需求相关的差评。只取和商品本身有关的，物流、客服类投诉不算。
"""

import re
from typing import Any

from i18n import en as english
from i18n import is_english, t


_SOURCES = ("marketing_description", "official_faq", "user_review")
_SERVICE_COMPLAINT_WORDS = ("客服", "物流", "快递", "发货", "退货", "退款", "售后", "包装", "少发", "发错", "仓库")
_SENTENCE_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?")
MAX_POINTS = 2
MAX_CHARS = 110
MAX_ENGLISH_CHARS = 240


def _body(chunk: Any) -> str:
    """去掉建索引时加的商品前缀，只留正文。"""
    document = getattr(chunk, "document", "") or ""
    if (getattr(chunk, "metadata", {}) or {}).get("dataset") == "amazon_reviews_2023":
        return document.strip()
    for marker in ("回答：", "内容："):
        if marker in document:
            return document.split(marker, 1)[1].strip()
    return ""


def _terms(text: str) -> set[str]:
    """Comparison units: words for English, character pairs for Chinese."""
    if is_english():
        return {word for word in re.findall(r"[a-z0-9']+", (text or "").casefold()) if word not in english.NAME_STOPWORDS}
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def overlap(text: str, query: str) -> int:
    """How many terms of the request appear in this text."""
    return len(_terms(text) & _terms(query))


def relevant_excerpt(text: str, query: str, limit: int | None = None) -> str:
    """取和需求重合最多的句子，保持原文顺序，不超过长度上限。"""
    limit = limit or (MAX_ENGLISH_CHARS if is_english() else MAX_CHARS)
    sentences = [s.strip() for s in (english.SENTENCE_RE if is_english() else _SENTENCE_RE).findall(text) if s.strip()]
    if not sentences:
        return text[:limit]
    wanted = _terms(query)
    ranked = sorted(
        range(len(sentences)),
        key=lambda i: (-len(_terms(sentences[i]) & wanted), i),
    )
    picked: list[int] = []
    used = 0
    for index in ranked:
        if picked and used + len(sentences[index]) > limit:
            continue
        picked.append(index)
        used += len(sentences[index])
        if used >= limit:
            break
    excerpt = ("" if not is_english() else " ").join(sentences[i] for i in sorted(picked))
    return excerpt[:limit]


def _about_service(text: str) -> bool:
    if is_english():
        return english.SERVICE_COMPLAINT_RE.search(text) is not None
    return any(word in text for word in _SERVICE_COMPLAINT_WORDS)


def _is_negative_review(chunk: Any) -> bool:
    meta = getattr(chunk, "metadata", {}) or {}
    return meta.get("chunk_type") == "user_review" and meta.get("polarity") == "negative"


def card_evidence(hit: Any, query: str) -> dict[str, Any] | None:
    """chunk 已按相关性排好序，优先取不同来源的依据。没有可用正文时返回 None。"""
    points: list[dict[str, str]] = []
    risk: str | None = None
    seen_sources: set[str] = set()
    backups: list[dict[str, str]] = []
    for chunk in getattr(hit, "evidence", None) or []:
        source = (getattr(chunk, "metadata", {}) or {}).get("chunk_type")
        if source not in _SOURCES:
            continue
        body = _body(chunk)
        if not body:
            continue
        if _is_negative_review(chunk):
            if risk is None and not _about_service(body):
                risk = relevant_excerpt(body, query)
            continue
        point = {"source": t(f"source.{source}"), "text": relevant_excerpt(body, query)}
        if source in seen_sources:
            backups.append(point)
        elif len(points) < MAX_POINTS:
            seen_sources.add(source)
            points.append(point)
    points.extend(backups[: MAX_POINTS - len(points)])
    if not points and not risk:
        return None
    out: dict[str, Any] = {"points": points}
    if risk:
        out["risk"] = risk
    return out


def product_claims(product_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """Knowledge-graph claims about one product, or nothing when the graph is unavailable."""
    from catalog_config import PROFILE

    if PROFILE.search_backend != "catalog":
        return []
    from search.search_service import get_search_service

    graph = get_search_service().graph
    return graph.claims(product_id, limit) if graph is not None else []
