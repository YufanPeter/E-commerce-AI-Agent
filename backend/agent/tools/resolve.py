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


def resolve_one(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    use_llm: bool = True,
) -> int | None:
    """Resolve one referenced candidate and return its zero-based index.

    Resolution proceeds through ordinal, unique name, then LLM disambiguation. Include
    title, brand, category, and subcategory in each candidate for semantic resolution.
    """
    if not candidates:
        return None

    indices = resolve_indices(query, len(candidates))
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
    for i in resolve_indices(query, len(candidates)):
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
