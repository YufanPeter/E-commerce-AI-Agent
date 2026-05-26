from __future__ import annotations

"""ParsedQuery → Chroma where 子句的适配层。

承担两层职责：
1. 字段映射：把语义化的 ParsedQuery 翻成 Chroma 的 MongoDB 风格语法。
2. 短路处理：没有任何硬过滤时返回 None，避免传一个空 dict 误触 Chroma 校验。

不在这里做的事：
- 价格 / 品牌的合法性校验（LLM enum + ParsedQuery dataclass 已经守门）
- 软偏好 / 否定成分（不是 Chroma 能干的活，留给后置过滤 / 重排）
"""

from typing import Any

from search.query_understanding import ParsedQuery, expand_brands


def build_chroma_where(parsed: ParsedQuery) -> dict[str, Any] | None:
    """根据 ParsedQuery.hard_filters 构造 Chroma collection.query 的 where。

    Chroma 语法约束（重要）：
        - 单条件可以直接 {"field": value}
        - 多条件必须显式用 {"$and": [...]} 包裹，不能简单合并 dict
        - $in / $nin 的值必须非空列表

    品牌处理：ParsedQuery 里存的是 canonical 名（如 "耐克"），但 Chroma
    metadata 里同一品牌可能有多种写法（"耐克" / "Nike"）。这里用
    expand_brands 把 canonical 展回所有变体，保证不漏召回。
    """
    clauses: list[dict[str, Any]] = []

    if parsed.category:
        clauses.append({"category": parsed.category})
    if parsed.sub_category:
        clauses.append({"sub_category": parsed.sub_category})

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
