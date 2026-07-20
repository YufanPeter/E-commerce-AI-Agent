from __future__ import annotations

"""Adapter from ``ParsedQuery`` to a Chroma ``where`` clause.

It maps semantic fields to Chroma's MongoDB-like syntax and returns ``None`` when no hard
filter exists, avoiding validation of an empty dictionary.

Price and brand validity are already checked upstream. Soft preferences and free-text
ingredient exclusions remain postfilters, while structured category exclusions use
``$nin`` here.
"""

from typing import Any

from search.query_understanding import ParsedQuery, expand_brands


def build_chroma_where(parsed: ParsedQuery) -> dict[str, Any] | None:
    """Build a Chroma query filter from ``ParsedQuery.hard_filters``.

    One condition may be direct, several require explicit ``$and``, and ``$in``/``$nin``
    values must be non-empty lists.

    Canonical brands expand to every metadata alias to preserve recall.
    """
    clauses: list[dict[str, Any]] = []

    if parsed.category:
        clauses.append({"category": parsed.category})
    if parsed.sub_category:
        clauses.append({"sub_category": parsed.sub_category})

    # Exclude structured categories during recall so disallowed items never enter candidates.
    if parsed.sub_category_exclude:
        clauses.append({"sub_category": {"$nin": list(parsed.sub_category_exclude)}})
    if parsed.category_exclude:
        clauses.append({"category": {"$nin": list(parsed.category_exclude)}})

    if parsed.min_price is not None:
        clauses.append({"base_price": {"$gte": float(parsed.min_price)}})
    if parsed.max_price is not None:
        clauses.append({"base_price": {"$lte": float(parsed.max_price)}})

    if parsed.brand_include:
        clauses.append({"brand": {"$in": expand_brands(parsed.brand_include)}})
    if parsed.brand_exclude:
        clauses.append({"brand": {"$nin": expand_brands(parsed.brand_exclude)}})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}
