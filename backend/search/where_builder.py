from __future__ import annotations

"""Adapter from ``ParsedQuery`` to a Chroma ``where`` clause.

It maps semantic fields to Chroma's MongoDB-like syntax and returns ``None`` when no hard
filter exists, avoiding validation of an empty dictionary.

Price and brand validity are already checked upstream. Soft preferences and free-text
ingredient exclusions remain postfilters, while structured category exclusions use
``$nin`` here.
"""

from typing import Any

from rag.vector_index import Condition, to_chroma_where
from search.query_understanding import ParsedQuery, expand_brands, expand_sub_categories


def build_filters(parsed: ParsedQuery) -> list[Condition]:
    """Translate ``ParsedQuery.hard_filters`` into backend-neutral conditions.

    Canonical brands and catalog groups expand to every stored value to preserve recall.
    """
    filters: list[Condition] = []

    if parsed.category:
        filters.append(Condition("category", "eq", parsed.category))
    if parsed.sub_category:
        labels = expand_sub_categories([parsed.sub_category])
        filters.append(Condition("sub_category", "eq", labels[0]) if len(labels) == 1 else Condition("sub_category", "in", labels))

    # Exclude structured categories during recall so disallowed items never enter candidates.
    if parsed.sub_category_exclude:
        filters.append(Condition("sub_category", "nin", expand_sub_categories(list(parsed.sub_category_exclude))))
    if parsed.category_exclude:
        filters.append(Condition("category", "nin", list(parsed.category_exclude)))

    # 价格按 SKU 区间判断：预算上限只要有一个规格买得起（min_price ≤ 预算）就召回，
    # 下限则要求最贵的规格也到得了（max_price ≥ 下限）。base_price 既不是最低也
    # 不是最高，用它过滤会把"有便宜规格的商品"误杀、把"只有贵规格的"误放。
    if parsed.min_price is not None:
        filters.append(Condition("max_price", "gte", float(parsed.min_price)))
    if parsed.max_price is not None:
        filters.append(Condition("min_price", "lte", float(parsed.max_price)))

    if parsed.brand_include:
        filters.append(Condition("brand", "in", expand_brands(parsed.brand_include)))
    if parsed.brand_exclude:
        filters.append(Condition("brand", "nin", expand_brands(parsed.brand_exclude)))
    return filters


def build_chroma_where(parsed: ParsedQuery) -> dict[str, Any] | None:
    """Build a Chroma query filter from ``ParsedQuery.hard_filters``."""
    return to_chroma_where(build_filters(parsed))
