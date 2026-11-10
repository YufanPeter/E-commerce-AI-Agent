from __future__ import annotations

"""从检索命中的 chunk 里挑出能支撑推荐理由的事实。

推荐解说如果只有标题可看，模型就只能写「性能够用又不贵」这种放哪款都成立的话。
这里把和用户需求最相关的原文句子交给 composer，让每句解说都有出处。

- points：卖点依据，来自商品描述、官方问答、非差评的用户评价。
- risk：一条和需求相关的差评。只取和商品本身有关的，物流、客服类投诉不算。
"""

import re
from typing import Any


_SOURCE_LABELS = {
    "marketing_description": "商品描述",
    "official_faq": "官方问答",
    "user_review": "用户评价",
}
_SERVICE_COMPLAINT_WORDS = ("客服", "物流", "快递", "发货", "退货", "退款", "售后", "包装", "少发", "发错", "仓库")
_SENTENCE_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?")
MAX_POINTS = 2
MAX_CHARS = 110


def _body(chunk: Any) -> str:
    """去掉建索引时加的商品前缀，只留正文。"""
    document = getattr(chunk, "document", "") or ""
    if (getattr(chunk, "metadata", {}) or {}).get("dataset") == "amazon_reviews_2023":
        return document.strip()
    for marker in ("回答：", "内容："):
        if marker in document:
            return document.split(marker, 1)[1].strip()
    return ""


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _relevant_excerpt(text: str, query: str) -> str:
    """取和需求重合最多的句子，保持原文顺序，不超过长度上限。"""
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]
    if not sentences:
        return text[:MAX_CHARS]
    wanted = _bigrams(query)
    ranked = sorted(
        range(len(sentences)),
        key=lambda i: (-len(_bigrams(sentences[i]) & wanted), i),
    )
    picked: list[int] = []
    used = 0
    for index in ranked:
        if picked and used + len(sentences[index]) > MAX_CHARS:
            continue
        picked.append(index)
        used += len(sentences[index])
        if used >= MAX_CHARS:
            break
    excerpt = "".join(sentences[i] for i in sorted(picked))
    return excerpt[:MAX_CHARS]


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
        if source not in _SOURCE_LABELS:
            continue
        body = _body(chunk)
        if not body:
            continue
        if _is_negative_review(chunk):
            if risk is None and not any(word in body for word in _SERVICE_COMPLAINT_WORDS):
                risk = _relevant_excerpt(body, query)
            continue
        if (getattr(chunk, "metadata", {}) or {}).get("dataset") == "amazon_reviews_2023":
            point = {"source": "Amazon listing", "text": body[:240]}
        else:
            point = {"source": _SOURCE_LABELS[source], "text": _relevant_excerpt(body, query)}
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
