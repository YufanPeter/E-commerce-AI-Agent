"""看更多：沿用上一轮条件、不重复已展示的商品、不再问 router。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.intent_router import IntentDecision
from agent.orchestrator import Agent, _compose_step_text, _filter_summary, _tool_step_text
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.recommend import RecommendTool
from agent.tools.refine import RefineTool, parse_more_request
from search.query_understanding import ParsedQuery
from search.search_service import SearchResult


def _hit(product_id):
    return SimpleNamespace(
        product_id=product_id, title=product_id, brand="", sub_category="耳机", category="数码电子",
        score=1.0, price=100.0, min_price=100.0, max_price=100.0,
        to_dict=lambda: {"product_id": product_id},
    )


class _Service:
    """按分数顺序返回固定商品，并记录每次检索的参数。"""

    def __init__(self, ids):
        self.ids = ids
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        parsed = kwargs.get("parsed") or ParsedQuery(original_query=query, retrieval_query=query)
        limit = kwargs.get("top_k_products", 10)
        return SearchResult(
            parsed=parsed,
            hits=[_hit(i) for i in self.ids[:limit]],
            raw_chunk_count=0,
            filtered_chunk_count=0,
        )


@pytest.fixture
def tool_setup():
    def build(ids):
        service = _Service(ids)
        recommend = RecommendTool(search_service=service)
        recommend._attach_price_displays = lambda *a, **k: None
        return service, RefineTool(recommend=recommend)
    with patch("agent.tools.recommend._to_product_card", lambda h, *_: {"product_id": h.product_id}):
        yield build


def _session_after_first_batch(ids):
    session = AgentSession()
    base = ParsedQuery(original_query="耳机", retrieval_query="耳机", max_price=300.0, sub_category="耳机")
    session.remember_search(base.to_dict(), [{"product_id": i, "title": i} for i in ids])
    return session


@pytest.mark.parametrize("query,expected", [
    ("更多", ""),
    ("还有别的吗", ""),
    ("换一批", ""),
    ("再看看？", ""),
    ("看看更多的", ""),
    ("更多轻薄便携款", "轻薄便携款"),
    ("还有便宜点的吗", None),
    ("再看看华为的耳机", None),
    ("推荐耳机", None),
    ("", None),
])
def test_only_pure_more_requests_are_recognised(query, expected):
    assert parse_more_request(query) == expected


def test_more_excludes_shown_products_and_keeps_previous_conditions(tool_setup):
    service, refine = tool_setup(["a", "b", "c", "d"])
    session = _session_after_first_batch(["a", "b"])
    result = refine.run("更多", session, {"more_label": ""})

    assert [p["product_id"] for p in result.payload["products"]] == ["c", "d"]
    assert service.calls[-1]["parsed"].max_price == 300.0
    assert service.calls[-1]["base"] is None
    # 放大了召回，否则已展示商品会占满名额
    assert service.calls[-1]["top_k_products"] > 2
    assert "不重复" in result.composer_hint
    assert session.recall_seen() == ["a", "b", "c", "d"]
    # "第一个"指向这一批
    assert [h["product_id"] for h in session.recall_hits()] == ["c", "d"]


def test_section_label_is_merged_without_reparsing(tool_setup):
    service, refine = tool_setup(["a", "c"])
    session = _session_after_first_batch(["a"])
    refine.run("轻薄便携款", session, {"more_label": "轻薄便携款"})

    parsed = service.calls[-1]["parsed"]
    assert "轻薄便携款" in parsed.soft_terms
    assert parsed.retrieval_query.startswith("轻薄便携款")
    assert parsed.sub_category == "耳机"


def test_nothing_new_keeps_references_and_tells_composer(tool_setup):
    _, refine = tool_setup(["a", "b"])
    session = _session_after_first_batch(["a", "b"])
    result = refine.run("更多", session, {"more_label": ""})

    assert result.payload["products"] == []
    assert "都已经给用户看过" in result.composer_hint
    assert [h["product_id"] for h in session.recall_hits()] == ["a", "b"]


def test_new_search_resets_what_counts_as_seen():
    session = _session_after_first_batch(["a"])
    session.remember_search({"q": 1}, [{"product_id": "x", "title": "x"}])
    assert session.recall_seen() == ["x"]


def test_section_after_multi_need_search_is_searched_on_its_own(tool_setup):
    service, refine = tool_setup(["a", "c"])
    session = _session_after_first_batch(["a"])
    session.set("last_search_multi", True)
    refine.run("防晒", session, {"more_label": "防晒"})

    assert service.calls[-1]["query"] == "防晒"
    assert "parsed" not in service.calls[-1]


def test_see_more_skips_router_and_passes_label_to_tool():
    captured = {}

    class Refine:
        name = "refine"

        def run(self, query, session, slots):
            captured.update(query=query, slots=slots)
            return ToolResult(tool_name="refine", payload={}, narrative_override="好", needs_composer=False)

    tools = {n: MagicMock() for n in ("recommend", "compare", "product_detail", "cart", "clarify", "fallback")}
    tools["refine"] = Refine()
    session = _session_after_first_batch(["a"])
    with patch("agent.orchestrator.route") as route:
        list(Agent(tools=tools).handle_turn_stream("更多轻薄便携款", session))
    route.assert_not_called()
    assert captured == {"query": "轻薄便携款", "slots": {"more_label": "轻薄便携款"}}


def test_more_without_a_previous_search_goes_through_router():
    tools = {n: MagicMock() for n in ("recommend", "refine", "compare", "product_detail", "cart", "clarify", "fallback")}
    tools["fallback"].run.return_value = ToolResult(
        tool_name="fallback", payload={}, narrative_override="好", needs_composer=False,
    )
    decision = IntentDecision(tool="fallback", rewritten_query="更多", confidence="low", reasoning="")
    with patch("agent.orchestrator.route", return_value=decision) as route:
        list(Agent(tools=tools).handle_turn_stream("更多", AgentSession()))
    route.assert_called_once()


class TestStepText:
    def _decision(self, tool, query):
        return IntentDecision(tool=tool, rewritten_query=query, confidence="high", reasoning="")

    @pytest.mark.parametrize("rewritten,expected", [
        ("推荐几款适合敏感肌的防晒", "正在搜索适合敏感肌的防晒…"),
        ("帮我找一下200元以内的蓝牙耳机吧", "正在搜索200元以内的蓝牙耳机…"),
        ("点心礼盒", "正在搜索点心礼盒…"),
        ("我想要跑鞋", "正在搜索跑鞋…"),
    ])
    def test_search_step_says_what_is_being_looked_for(self, rewritten, expected):
        assert _tool_step_text(self._decision("recommend", rewritten), rewritten) == expected

    def test_long_query_is_cut(self):
        text = _tool_step_text(self._decision("recommend", "键" * 60), "x")
        assert text == "正在搜索" + "键" * 24 + "…"

    def test_other_tools_use_their_fixed_text(self):
        assert _tool_step_text(self._decision("compare", "a 和 b"), "x") == "正在对比商品…"

    def test_see_more_step(self):
        assert _tool_step_text(self._decision("refine", "x"), "更多") == "正在找更多商品…"
        assert _tool_step_text(self._decision("refine", "轻薄款"), "更多轻薄款") == "正在找更多轻薄款…"

    def test_filter_summary_only_mentions_real_conditions(self):
        assert _filter_summary({}) == ""
        assert _filter_summary({
            "sub_category": "真无线耳机", "max_price": 300.0, "brand_include": ["华为"], "negative_ingredients": ["酒精"],
        }) == "真无线耳机 · 华为 · 300 元以内 · 不要酒精"
        assert _filter_summary({"category": "美妆护肤", "min_price": 100.0, "max_price": 200.0}) == "美妆护肤 · 100-200 元"

    def test_compose_step_reports_actual_result(self):
        found = ToolResult(tool_name="recommend", payload={
            "products": [{}, {}], "debug": {"parsed": {"category": "数码电子", "max_price": 500.0}},
        })
        assert _compose_step_text(found) == "按「数码电子 · 500 元以内」找到 2 款，正在写推荐理由…"
        assert _compose_step_text(ToolResult(tool_name="recommend", payload={"products": [{}]})) == "找到 1 款，正在写推荐理由…"
        assert _compose_step_text(ToolResult(tool_name="recommend", payload={"products": []})).startswith("没有找到")
        assert _compose_step_text(ToolResult(tool_name="compare", payload={})) == "正在生成对比总结…"
