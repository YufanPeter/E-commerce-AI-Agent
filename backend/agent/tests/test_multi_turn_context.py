"""Regression tests for refining multi-turn context.

When a user names only a brand after asking for running shoes, the next query must inherit the
product category instead of degrading to a generic brand search.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.refine import RefineTool
from search.query_understanding import ParsedQuery


# --------------------------- ParsedQuery.merge_base ---------------------------


def test_merge_inherits_category_when_only_brand_given():
    base = ParsedQuery(original_query="跑鞋", category="服饰运动",
                       sub_category="跑鞋", retrieval_query="跑鞋")
    cur = ParsedQuery(original_query="Adidas", brand_include=["Adidas"],
                      retrieval_query="Adidas")

    merged = cur.merge_base(base)

    assert merged.brand_include == ["Adidas"]
    assert merged.category == "服饰运动"          # Inherited
    assert merged.sub_category == "跑鞋"          # Inherited
    assert merged.needs_clarification is False
    # Vector-retrieval text should include both the brand and the previous category.
    assert "Adidas" in merged.retrieval_query
    assert "跑鞋" in merged.retrieval_query


def test_merge_brand_replaces_not_accumulates():
    base = ParsedQuery(original_query="Adidas 跑鞋", category="服饰运动",
                       brand_include=["Adidas"], retrieval_query="Adidas 跑鞋")
    cur = ParsedQuery(original_query="换成Nike", brand_include=["Nike"],
                      retrieval_query="Nike")

    merged = cur.merge_base(base)

    assert merged.brand_include == ["Nike"]       # Replace rather than accumulate brands.
    assert merged.category == "服饰运动"


def test_merge_price_refine_keeps_prior_constraints():
    base = ParsedQuery(original_query="Adidas 跑鞋", category="服饰运动",
                       sub_category="跑鞋", brand_include=["Adidas"],
                       retrieval_query="Adidas 跑鞋")
    cur = ParsedQuery(original_query="便宜点", max_price=500.0)

    merged = cur.merge_base(base)

    assert merged.max_price == 500.0
    assert merged.brand_include == ["Adidas"]     # A price refinement must not clear the brand.
    assert merged.sub_category == "跑鞋"


def test_merge_negative_and_soft_terms_accumulate():
    base = ParsedQuery(original_query="精华", negative_ingredients=["酒精"],
                       soft_terms=["保湿"])
    cur = ParsedQuery(original_query="不要香精", negative_ingredients=["香精"],
                      soft_terms=["抗老"])

    merged = cur.merge_base(base)

    assert set(merged.negative_ingredients) == {"酒精", "香精"}
    assert set(merged.soft_terms) == {"保湿", "抗老"}


def test_from_dict_ignores_derived_hard_filters():
    """Allow `from_dict` to restore data containing derived `hard_filters` from `to_dict`."""
    pq = ParsedQuery(original_query="跑鞋", category="服饰运动")
    restored = ParsedQuery.from_dict(pq.to_dict())
    assert restored == pq


# --------------------------- RefineTool base injection ---------------------------


class _SpyRecommend:
    """Record slots passed from RefineTool to recommendations to verify base injection."""

    def __init__(self) -> None:
        self.received_slots: dict[str, Any] | None = None

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        self.received_slots = slots
        return ToolResult(tool_name="recommend", payload={"query": query, "products": []})


def test_refine_rebuilds_base_from_structured_memory():
    session = AgentSession()
    session.remember_search(
        ParsedQuery(original_query="跑鞋", category="服饰运动",
                    sub_category="跑鞋", retrieval_query="跑鞋").to_dict(),
        [{"product_id": "p_clothes_001", "title": "某跑鞋"}],
    )

    spy = _SpyRecommend()
    RefineTool(recommend=spy).run("Adidas", session, slots={})

    base = spy.received_slots["base_parsed"]
    assert isinstance(base, ParsedQuery)
    assert base.category == "服饰运动"
    assert base.sub_category == "跑鞋"


def test_refine_without_memory_falls_back_to_plain_recommend():
    session = AgentSession()  # No previous-turn memory.

    spy = _SpyRecommend()
    RefineTool(recommend=spy).run("Adidas", session, slots={})

    # Without a base, do not inject `base_parsed`; fall back to a normal recommendation.
    assert "base_parsed" not in (spy.received_slots or {})


def test_refine_tolerates_legacy_string_memory():
    session = AgentSession()
    session.set("last_parsed_query", "跑鞋")  # Older versions stored a string.

    spy = _SpyRecommend()
    RefineTool(recommend=spy).run("Adidas", session, slots={})

    assert "base_parsed" not in (spy.received_slots or {})
