"""多轮上下文细化的回归测试。

核心场景：上一轮"推荐跑鞋"，本轮只说品牌"Adidas"，不能退化成"泛搜 Adidas"，
而要理解为"Adidas 跑鞋"——品类被继承、品牌被叠加。
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
    assert merged.category == "服饰运动"          # 继承
    assert merged.sub_category == "跑鞋"          # 继承
    assert merged.needs_clarification is False
    # 向量检索文本同时含品牌 + 上一轮品类
    assert "Adidas" in merged.retrieval_query
    assert "跑鞋" in merged.retrieval_query


def test_merge_brand_replaces_not_accumulates():
    base = ParsedQuery(original_query="Adidas 跑鞋", category="服饰运动",
                       brand_include=["Adidas"], retrieval_query="Adidas 跑鞋")
    cur = ParsedQuery(original_query="换成Nike", brand_include=["Nike"],
                      retrieval_query="Nike")

    merged = cur.merge_base(base)

    assert merged.brand_include == ["Nike"]       # 替换，而非 [Adidas, Nike]
    assert merged.category == "服饰运动"


def test_merge_price_refine_keeps_prior_constraints():
    base = ParsedQuery(original_query="Adidas 跑鞋", category="服饰运动",
                       sub_category="跑鞋", brand_include=["Adidas"],
                       retrieval_query="Adidas 跑鞋")
    cur = ParsedQuery(original_query="便宜点", max_price=500.0)

    merged = cur.merge_base(base)

    assert merged.max_price == 500.0
    assert merged.brand_include == ["Adidas"]     # 价格细化不该清掉品牌
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
    """to_dict() 会塞入派生的 hard_filters，from_dict 必须能宽容还原。"""
    pq = ParsedQuery(original_query="跑鞋", category="服饰运动")
    restored = ParsedQuery.from_dict(pq.to_dict())
    assert restored == pq


# --------------------------- RefineTool 注入 base ---------------------------


class _SpyRecommend:
    """记录 RefineTool 透传给 recommend 的 slots，验证 base 被正确注入。"""

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
    session = AgentSession()  # 无任何上一轮记忆

    spy = _SpyRecommend()
    RefineTool(recommend=spy).run("Adidas", session, slots={})

    # 没有 base 时不注入 base_parsed，退回普通 recommend
    assert "base_parsed" not in (spy.received_slots or {})


def test_refine_tolerates_legacy_string_memory():
    session = AgentSession()
    session.set("last_parsed_query", "跑鞋")  # 旧版本曾存字符串

    spy = _SpyRecommend()
    RefineTool(recommend=spy).run("Adidas", session, slots={})

    assert "base_parsed" not in (spy.received_slots or {})
