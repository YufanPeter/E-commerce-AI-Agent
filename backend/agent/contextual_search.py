from __future__ import annotations

"""Context-aware retrieval plans for multi-turn shopping conversations.

An ordinary refinement keeps filtering within the previous category, but requests
such as “搭配一件上衣” or “再看看运动鞋” change the current target. The previous
category should then provide context only, not a retrieval term or hard filter.

This module provides a lightweight deterministic first stage. A small taxonomy and
alias table identifies complement and category-pivot requests and emits a search plan.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from search.query_understanding import ParsedQuery


SearchMode = Literal["complement", "pivot"]


@dataclass(frozen=True)
class ContextualSearchPlan:
    """Structured plan for a cross-category or complementary follow-up."""

    mode: SearchMode
    target_query: str
    target_terms: list[str]
    target_sub_categories: list[str] = field(default_factory=list)
    anchor_query: str | None = None
    anchor_sub_category: str | None = None
    exclude_sub_categories: list[str] = field(default_factory=list)
    relation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "target_query": self.target_query,
            "target_terms": self.target_terms,
            "target_sub_categories": self.target_sub_categories,
            "anchor_query": self.anchor_query,
            "anchor_sub_category": self.anchor_sub_category,
            "exclude_sub_categories": self.exclude_sub_categories,
            "relation": self.relation,
        }


# Natural-language aliases mapped to one or more catalog subcategories.
_TARGET_ALIASES: dict[str, list[str]] = {
    "运动上衣": ["短袖T恤", "速干T恤", "卫衣"],
    "上衣": ["短袖T恤", "速干T恤", "卫衣"],
    "短袖": ["短袖T恤", "速干T恤"],
    "t恤": ["短袖T恤", "速干T恤"],
    "T恤": ["短袖T恤", "速干T恤"],
    "速干衣": ["速干T恤"],
    "速干T恤": ["速干T恤"],
    "卫衣": ["卫衣"],
    "运动鞋": ["跑步鞋", "篮球鞋", "徒步鞋"],
    "鞋子": ["跑步鞋", "篮球鞋", "徒步鞋"],
    "鞋": ["跑步鞋", "篮球鞋", "徒步鞋"],
    "跑鞋": ["跑步鞋"],
    "跑步鞋": ["跑步鞋"],
    "篮球鞋": ["篮球鞋"],
    "徒步鞋": ["徒步鞋"],
    "运动短裤": ["运动短裤"],
    "短裤": ["运动短裤"],
    "运动长裤": ["运动长裤"],
    "户外裤": ["户外裤"],
    "帽子": ["帽子"],
    "背包": ["背包"],
    "调味品": ["调味品"],
    "咖啡": ["咖啡"],
    "牛奶": ["牛奶"],
    "零食": ["坚果/零食"],
    "坚果": ["坚果/零食"],
    "防晒": ["防晒"],
    "面霜": ["面霜"],
    "精华": ["精华"],
    "洁面": ["洁面"],
    "化妆水": ["化妆水"],
}


_COMPLEMENT_WORDS = (
    "搭配", "配套", "配什么", "一起买", "一起搭", "适合配", "配一件",
    "配个", "搭一件", "搭个", "搭一套", "穿搭",
)

_PIVOT_WORDS = (
    "看看", "看一下", "看下", "再看", "再看看", "换成", "换个",
    "换一类", "其他品类", "别的品类", "其他类", "另一个品类",
)


def detect_contextual_plan(
    query: str,
    base: ParsedQuery | None,
) -> ContextualSearchPlan | None:
    """Detect complement or category-pivot follow-ups.

    Return ``None`` to continue through the regular recommend/refine path. A plan is
    returned only when the current target is recognized and differs from the prior
    subcategory.
    """
    text = (query or "").strip()
    if not text:
        return None

    target = _find_target_term(text, base)
    if target is None:
        return None

    target_term, target_subs = target
    anchor_sub = base.sub_category if base else None
    anchor_query = (base.retrieval_query or base.original_query) if base else None
    exclude_subs = [anchor_sub] if anchor_sub and anchor_sub not in target_subs else []

    if _has_any(text, _COMPLEMENT_WORDS):
        return ContextualSearchPlan(
            mode="complement",
            target_query=target_term,
            target_terms=[target_term],
            target_sub_categories=target_subs,
            anchor_query=anchor_query,
            anchor_sub_category=anchor_sub,
            exclude_sub_categories=exclude_subs,
            relation="搭配",
        )

    # A new target under an existing context usually means a category pivot. Trigger
    # words reduce false positives; short inputs such as "sports top" also count.
    if base is not None and (_has_any(text, _PIVOT_WORDS) or _is_short_target_only(text, target_term)):
        return ContextualSearchPlan(
            mode="pivot",
            target_query=target_term,
            target_terms=[target_term],
            target_sub_categories=target_subs,
            anchor_query=anchor_query,
            anchor_sub_category=anchor_sub,
            exclude_sub_categories=exclude_subs,
            relation="换目标品类",
        )

    return None


def _find_target_term(
    text: str,
    base: ParsedQuery | None,
) -> tuple[str, list[str]] | None:
    """Find the longest target alias, excluding the previous subcategory."""
    anchor_sub = base.sub_category if base else None
    for term in sorted(_TARGET_ALIASES, key=len, reverse=True):
        if term not in text:
            continue
        subs = _TARGET_ALIASES[term]
        if anchor_sub and subs == [anchor_sub]:
            continue
        return term, subs
    return None


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _is_short_target_only(text: str, target_term: str) -> bool:
    compact = text.replace(" ", "")
    return compact == target_term or (target_term in compact and len(compact) <= len(target_term) + 4)
