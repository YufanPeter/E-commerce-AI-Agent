from __future__ import annotations

"""多轮导购里的上下文检索计划。

普通 refine 是在上一轮同一品类里继续筛；但用户也会说“搭配一件上衣”
或“再看看运动鞋”。这两类本轮目标已经变了，上一轮品类只能作为语境，
不能进入检索词或硬过滤，否则就会出现“想看运动上衣却继续返回瑜伽裤”。

本模块先做一个轻量、确定性的第一阶段：用小型 taxonomy/别名表识别
complement（搭配/互补）和 pivot（换目标品类），产出检索计划。
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from search.query_understanding import ParsedQuery


SearchMode = Literal["complement", "pivot"]


@dataclass(frozen=True)
class ContextualSearchPlan:
    """一次跨品类/搭配式追问的结构化计划。"""

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


# 用户自然说法 → 库中真实子类目。值可以覆盖多个真实子类目。
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
    """识别 complement / pivot 追问。

    返回 None 表示继续走原来的 recommend/refine 逻辑。
    只在能识别出“本轮目标词”且该目标不同于上一轮子类目时返回计划。
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

    # 有上一轮上下文，并且本轮出现一个不同于旧品类的目标词：通常是换目标品类。
    # 加 pivot 触发词能降低误伤；短输入如“运动上衣”也算用户在当前上下文里转向。
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
    """在 query 里找目标词，优先最长别名，并跳过上一轮相同子类目。"""
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