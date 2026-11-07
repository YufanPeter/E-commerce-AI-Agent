from __future__ import annotations

"""Layered product-reference resolver shared by detail, compare, and cart tools.

The three tools once implemented candidate selection independently, and only detail
had LLM-assisted semantic disambiguation. This shared resolver applies the same
deterministic-first, semantic-fallback strategy across tools and categories.

Deterministic shortcuts handle ordinals and unique name matches. The LLM is invoked
only for genuine ambiguity (zero or multiple name matches). If it still cannot decide,
the tool receives no selection and can ask a clarifying question.

The LLM is an enhancement, not a dependency: failures always fall back to rules.
"""

import logging
from typing import Any

from agent.tools.llm_match import llm_pick_candidate, llm_pick_candidates
from agent.tools.reference import resolve_by_name, resolve_indices

logger = logging.getLogger(__name__)


def focus_index_in(hits: list[dict[str, Any]], session: Any) -> int | None:
    """当前聚焦商品（``last_focus_product_id``）在 hits 里的下标；没有则 None。

    「上一个 / 下一个」这类相对指代以它为锚点。"""
    focus_id = session.get("last_focus_product_id") if session is not None else None
    if not focus_id:
        return None
    for i, hit in enumerate(hits):
        if str(hit.get("product_id")) == str(focus_id):
            return i
    return None


def enrich_candidates(
    candidates: list[dict[str, Any]],
    store: Any,
) -> list[dict[str, Any]]:
    """给候选补齐 brand / category / sub_category / options（SKU 规格值），三个工具共用。

    options 形如 {"颜色": ["黑色", "白色"], "尺码": ["41", "42"]}，只保留取值多于一个
    的维度——这些才是「黑色那个」「42 码的」能用来区分的属性；规则匹配
    （resolve_by_name）和 LLM 消歧（llm_match）都会读它。
    富化失败（取详情异常）不影响主流程——仍可只凭 title 消歧。"""
    enriched: list[dict[str, Any]] = []
    for cand in candidates:
        item = dict(cand)
        if "options" not in item and store is not None:
            try:
                detail = store.get_product_detail(item["product_id"])
            except Exception:  # noqa: BLE001
                detail = None
            if detail is not None:
                item.setdefault("title", detail.title)
                item["brand"] = detail.brand
                item["category"] = detail.category
                item["sub_category"] = detail.sub_category
                item["options"] = _sku_options(getattr(detail, "skus", None) or [])
        enriched.append(item)
    return enriched


def _sku_options(skus: list[Any]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for sku in skus:
        props = getattr(sku, "properties", None)
        if props is None and isinstance(sku, dict):
            props = sku.get("properties") or sku.get("options")
        if not isinstance(props, dict):
            continue
        for key, value in props.items():
            text = str(value).strip()
            if not text:
                continue
            bucket = values.setdefault(str(key), [])
            if text not in bucket:
                bucket.append(text)
    # 单值维度也要留着：一款手机只有「星芒黑」一个颜色，但正是它把这款和别的区分开；
    # 「是否在候选之间有区分度」由 resolve_by_name 在候选集合层面判断。
    return values


def resolve_one(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    use_llm: bool = True,
    focus_index: int | None = None,
) -> int | None:
    """Resolve one referenced candidate and return its zero-based index.

    分层：序号唯一（含「上一个 / 下一个」相对指代，以 focus_index 为锚）→
    名称 / 规格值唯一 →（歧义时）LLM 消歧。candidates 每项建议带
    title/brand/category/sub_category/options，LLM 消歧才有依据。
    """
    if not candidates:
        return None

    indices = resolve_indices(query, len(candidates), focus_index=focus_index)
    if indices:
        i = indices[0]
        if 0 <= i < len(candidates):
            return i

    named = resolve_by_name(query, candidates)
    if len(named) == 1:
        return named[0]

    # Zero or multiple name matches are ambiguous; ask the LLM to select one.
    if use_llm:
        picked = llm_pick_candidate(query, candidates)
        if picked is not None and 0 <= picked < len(candidates):
            return picked

    return None


def resolve_many(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    k: int = 2,
    use_llm: bool = True,
    focus_index: int | None = None,
) -> list[int]:
    """Resolve up to ``k`` referenced candidates, preserving order and uniqueness.

    Ordinals take priority. If fewer than ``k`` are selected, unambiguous name matches
    fill the gap; excessive name matches are semantically arbitrated by the LLM. When
    the LLM is unavailable, name matches are used as the fallback.
    """
    if not candidates or k <= 0:
        return []

    # Ordinals are deterministic and always take priority.
    picked: list[int] = []
    for i in resolve_indices(query, len(candidates), focus_index=focus_index):
        if 0 <= i < len(candidates) and i not in picked:
            picked.append(i)
    if len(picked) >= k:
        return picked[:k]

    name_hits = [i for i in resolve_by_name(query, candidates) if i not in picked]
    gap = k - len(picked)

    # Name matches that fit the remaining slots are unambiguous.
    if len(name_hits) <= gap:
        picked.extend(name_hits)
        if len(picked) >= k or not use_llm:
            return picked[:k]
        # Let the LLM fill any remaining slots.
        for i in llm_pick_candidates(query, candidates, k=k):
            if i not in picked:
                picked.append(i)
        return picked[:k]

    # Too many name matches indicate ambiguity; let the LLM arbitrate.
    if use_llm:
        llm_hits = [i for i in llm_pick_candidates(query, candidates, k=k) if i not in picked]
        if llm_hits:
            picked.extend(llm_hits)
            return picked[:k]
    picked.extend(name_hits)
    return picked[:k]
