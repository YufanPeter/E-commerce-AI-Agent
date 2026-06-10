"""单元测试：用 mock 覆盖 orchestrator 的全部分支 + edge case。

不依赖任何外部服务，可在 CI 中无脑跑。
覆盖：
- AgentSession 行为
- Clarify / Fallback / Recommend Tool
- AnswerComposer.compose / compose_stream
- Orchestrator.handle_turn happy / 降级
- Orchestrator.handle_turn_stream happy / 降级 / 事件契约
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.composer import AnswerComposer
from agent.intent_router import IntentDecision
from agent.orchestrator import Agent
from agent.session import AgentSession, DEFAULT_HISTORY_WINDOW
from agent.tools.base import ToolResult
from agent.tools.clarify import ClarifyTool
from agent.tools.cart import CartTool
from agent.tools.compare import CompareTool
from agent.tools.fallback import FallbackTool
from agent.tools.product_detail import ProductDetailTool
from agent.tools.recommend import RecommendTool
from agent.tools.refine import RefineTool
from search.query_decomposer import SubRequest
from search.query_understanding import ParsedQuery
from store.product_store import PriceRange, ProductDetail


# ---------- 工具：构造伪响应 ----------

def _fake_tool_response(tool: str, rewritten: str, conf: str = "high", reason: str = "ok") -> Any:
    """模拟 Ark function-calling 返回值的最小骨架。"""
    args = json.dumps({
        "tool": tool,
        "rewritten_query": rewritten,
        "confidence": conf,
        "reasoning": reason,
    })
    tool_call = SimpleNamespace(function=SimpleNamespace(arguments=args))
    msg = SimpleNamespace(tool_calls=[tool_call], content=None)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _fake_chat_response(text: str) -> Any:
    msg = SimpleNamespace(tool_calls=None, content=text)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _fake_stream_chunks(pieces: list[str]):
    """模拟 OpenAI stream=True 的迭代器：每个 chunk 一个 delta.content。"""
    for p in pieces:
        delta = SimpleNamespace(content=p)
        choice = SimpleNamespace(delta=delta, index=0, finish_reason=None)
        yield SimpleNamespace(choices=[choice])


def _detail(pid: str, title: str, brand: str = "品牌") -> ProductDetail:
    return ProductDetail(
        product_id=pid,
        title=title,
        brand=brand,
        category="数码电子",
        sub_category="智能手机",
        base_price=1000,
        image_path=None,
        image_url=None,
        price_range=PriceRange(min_price=1000, max_price=1200, sku_count=2),
        skus=[],
        marketing_description="",
        faqs=[],
        reviews=[],
    )


class _FakeProductStore:
    def __init__(self, details: list[ProductDetail]):
        self._by_id = {d.product_id: d for d in details}

    def get_product_detail(self, pid: str):
        return self._by_id.get(pid)


def _fake_comparison(details, focus="", timeout=None):
    return {
        "title": "对比",
        "products": [
            {"product_id": d.product_id, "title": d.title, "brand": d.brand, "image_url": d.image_url}
            for d in details
        ],
        "rows": [],
        "recommendation": "",
    }


class _StubSearchService:
    """模拟 SearchService，可控返回 hits 和 needs_clarification。"""
    def __init__(self, hits: list[dict] | None = None, needs_clarify: bool = False):
        self._hits = hits or []
        self._needs_clarify = needs_clarify
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, top_k_chunks: int = 50, top_k_products: int = 10, base=None):
        self.calls.append({"query": query, "top_k_products": top_k_products, "base": base})
        parsed = SimpleNamespace(
            needs_clarification=self._needs_clarify,
            category="美妆护肤",
            sub_category=None,
            max_price=500,
            brand_include=[],
            to_dict=lambda: {
                "category": "美妆护肤",
                "sub_category": None,
                "max_price": 500,
                "brand_include": [],
                "needs_clarification": self._needs_clarify,
            },
        )
        hit_objs = []
        for h in self._hits:
            hit_objs.append(SimpleNamespace(
                product_id=h["product_id"],
                title=h["title"],
                brand=h.get("brand", ""),
                category=h.get("category", "美妆护肤"),
                sub_category=h.get("sub_category", ""),
                base_price=h.get("base_price", 0),
                to_dict=lambda h=h: h,
            ))
        return SimpleNamespace(
            parsed=parsed,
            hits=hit_objs,
            raw_chunk_count=10,
            filtered_chunk_count=5,
        )


def _single_decomposer(query: str) -> list[SubRequest]:
    """测试用 stub：永远当作单需求，避免 RecommendTool.run 触发真实 LLM 拆解。"""
    return [SubRequest(label=query, query=query)]


def _make_agent(
    tool_search_hits: list[dict] | None = None,
    needs_clarify: bool = False,
) -> Agent:
    stub = _StubSearchService(hits=tool_search_hits, needs_clarify=needs_clarify)
    recommend = RecommendTool(search_service=stub, decomposer=_single_decomposer)
    return Agent(tools={
        "recommend": recommend,
        "refine": RefineTool(recommend=recommend),
        "compare": CompareTool(),
        "product_detail": ProductDetailTool(),
        "cart": CartTool(),
        "clarify": ClarifyTool(),
        "fallback": FallbackTool(),
    })


def _remember_base(session: AgentSession, sub_category: str, query: str | None = None) -> None:
    parsed = ParsedQuery(
        original_query=query or sub_category,
        category="服饰运动",
        sub_category=sub_category,
        retrieval_query=query or sub_category,
    )
    session.remember_search(parsed.to_dict(), [{"product_id": "prev", "title": sub_category}])


# ---------- Contextual search（refine / pivot / complement） ----------

class TestContextualSearchModes:
    def test_complement_targets_new_category_and_excludes_anchor(self):
        service = _StubSearchService(hits=[
            {"product_id": "old", "title": "Lululemon 瑜伽裤", "brand": "Lululemon", "sub_category": "瑜伽裤", "base_price": 800},
            {"product_id": "tee", "title": "Nike 运动短袖", "brand": "Nike", "sub_category": "短袖T恤", "base_price": 199},
            {"product_id": "hoodie", "title": "李宁连帽卫衣", "brand": "李宁", "sub_category": "卫衣", "base_price": 299},
        ])
        tool = RecommendTool(search_service=service, decomposer=_single_decomposer)
        session = AgentSession()
        _remember_base(session, "瑜伽裤")

        result = tool.run("推荐可以搭配瑜伽裤的运动上衣", session, {})

        assert service.calls[-1]["query"] == "运动上衣"
        assert service.calls[-1]["base"] is None
        assert result.payload["summary"]["mode"] == "complement"
        assert [p["sub_category"] for p in result.payload["products"]] == ["短袖T恤", "卫衣"]
        assert all(p["sub_category"] != "瑜伽裤" for p in result.payload["products"])
        assert result.payload["contextual_search"]["exclude_sub_categories"] == ["瑜伽裤"]

    def test_pivot_to_shoes_does_not_return_previous_yoga_pants(self):
        service = _StubSearchService(hits=[
            {"product_id": "old", "title": "Lululemon 瑜伽裤", "brand": "Lululemon", "sub_category": "瑜伽裤", "base_price": 800},
            {"product_id": "run", "title": "HOKA 跑步鞋", "brand": "HOKA", "sub_category": "跑步鞋", "base_price": 999},
            {"product_id": "basket", "title": "Nike 篮球鞋", "brand": "Nike", "sub_category": "篮球鞋", "base_price": 699},
        ])
        tool = RecommendTool(search_service=service, decomposer=_single_decomposer)
        session = AgentSession()
        _remember_base(session, "瑜伽裤")

        result = tool.run("再看看运动鞋", session, {})

        assert service.calls[-1]["query"] == "运动鞋"
        assert result.payload["summary"]["mode"] == "pivot"
        assert [p["sub_category"] for p in result.payload["products"]] == ["跑步鞋", "篮球鞋"]

    def test_refine_keeps_base_for_same_category_adjustment(self):
        service = _StubSearchService(hits=[
            {"product_id": "old", "title": "平价瑜伽裤", "brand": "优衣库", "sub_category": "瑜伽裤", "base_price": 199},
        ])
        tool = RecommendTool(search_service=service, decomposer=_single_decomposer)
        session = AgentSession()
        base = ParsedQuery(original_query="瑜伽裤", category="服饰运动", sub_category="瑜伽裤", retrieval_query="瑜伽裤")

        result = tool.run("再便宜点", session, {"base_parsed": base})

        assert service.calls[-1]["query"] == "再便宜点"
        assert service.calls[-1]["base"] == base
        assert "mode" not in result.payload["summary"]
        assert result.payload["products"][0]["sub_category"] == "瑜伽裤"

    def test_complement_no_target_hits_does_not_fallback_to_anchor(self):
        service = _StubSearchService(hits=[
            {"product_id": "old", "title": "Lululemon 瑜伽裤", "brand": "Lululemon", "sub_category": "瑜伽裤", "base_price": 800},
        ])
        tool = RecommendTool(search_service=service, decomposer=_single_decomposer)
        session = AgentSession()
        _remember_base(session, "瑜伽裤")

        result = tool.run("搭配一件运动上衣", session, {})

        assert result.payload["summary"]["mode"] == "complement"
        assert result.payload["summary"]["hit_count"] == 0
        assert result.payload["products"] == []
        assert "不要回退推荐上一轮品类" in (result.composer_hint or "")


# ---------- AgentSession 测试 ----------

class TestAgentSession:
    def test_basic_history(self):
        s = AgentSession()
        s.add_user("hi")
        s.add_assistant("hello")
        assert len(s.history) == 2

    def test_history_sliding_window(self):
        s = AgentSession(history_window=4)
        for i in range(10):
            s.add_user(f"q{i}")
        assert len(s.history) == 4
        assert s.history[0].content == "q6"
        assert s.history[-1].content == "q9"

    def test_default_window(self):
        s = AgentSession()
        assert s.history_window == DEFAULT_HISTORY_WINDOW

    def test_working_memory(self):
        s = AgentSession()
        s.set("k", 42)
        assert s.get("k") == 42
        assert s.get("missing", "x") == "x"

    def test_recent_text_with_empty(self):
        s = AgentSession()
        assert s.recent_text() == ""


# ---------- ClarifyTool / FallbackTool ----------

class TestStaticTools:
    def test_clarify_no_llm(self):
        r = ClarifyTool().run("随便看看", AgentSession(), {})
        assert r.tool_name == "clarify"
        assert r.needs_composer is False
        assert r.narrative_override and "品类" in r.narrative_override

    def test_fallback_no_llm(self):
        r = FallbackTool().run("天气", AgentSession(), {})
        assert r.tool_name == "fallback"
        assert r.needs_composer is False
        assert "导购" in r.narrative_override


# ---------- RecommendTool ----------

class TestRecommendTool:
    def test_with_hits(self):
        hits = [{"product_id": "p1", "title": "雅诗兰黛精华", "brand": "雅诗兰黛", "base_price": 480}]
        tool = RecommendTool(search_service=_StubSearchService(hits=hits), decomposer=_single_decomposer)
        session = AgentSession()
        r = tool.run("500内精华", session, {})
        assert r.tool_name == "recommend"
        assert len(r.payload["products"]) == 1
        # 精简后的商品卡只含展示字段，无 evidence/score 等内部字段
        p = r.payload["products"][0]
        assert p["product_id"] == "p1"
        assert p["title"] == "雅诗兰黛精华"
        assert p["price"] == 480
        assert "score" not in p and "evidence" not in p
        # debug 块仍保留原始数据，方便排查
        assert "parsed" in r.payload["debug"]
        assert len(r.payload["debug"]["hits_full"]) == 1
        # summary 提供高层摘要
        assert r.payload["summary"]["hit_count"] == 1
        assert session.get("last_hits") == [{"product_id": "p1", "title": "雅诗兰黛精华"}]
        assert "1 款商品" in r.composer_hint

    def test_zero_hits_hint(self):
        tool = RecommendTool(search_service=_StubSearchService(hits=[]), decomposer=_single_decomposer)
        r = tool.run("xxx", AgentSession(), {})
        assert "未命中" in r.composer_hint

    def test_needs_clarification_hint(self):
        tool = RecommendTool(
            search_service=_StubSearchService(hits=[], needs_clarify=True),
            decomposer=_single_decomposer,
        )
        r = tool.run("随便看看", AgentSession(), {})
        assert "模糊" in r.composer_hint

    def test_refine_path_skips_decompose(self):
        """有 base_parsed（refine）时不应触发拆解：注入一个会爆炸的 decomposer 验证。"""
        def _boom(_q: str):
            raise AssertionError("refine 路径不应调用 decomposer")

        hits = [{"product_id": "p1", "title": "雅诗兰黛精华", "base_price": 480}]
        tool = RecommendTool(search_service=_StubSearchService(hits=hits), decomposer=_boom)
        r = tool.run("再便宜点", AgentSession(), {"base_parsed": object()})
        assert r.tool_name == "recommend"
        assert "groups" not in r.payload  # 单需求路径不产出 groups

    def test_multi_intent_fan_out_and_grouping(self):
        """多需求：分别检索后按组聚合，products 扁平化，groups 保留分区。"""
        hits = [
            {"product_id": "p1", "title": "安热沙防晒", "base_price": 298},
            {"product_id": "p2", "title": "速干短袖", "base_price": 99},
        ]

        def _multi(_q: str):
            return [
                SubRequest(label="防晒", query="三亚海边 防晒霜"),
                SubRequest(label="衣服", query="三亚夏季 速干衣"),
            ]

        tool = RecommendTool(search_service=_StubSearchService(hits=hits), decomposer=_multi)
        session = AgentSession()
        r = tool.run("去三亚旅游推荐衣服和防晒", session, {})

        assert r.tool_name == "recommend"
        # groups 结构存在且含两个子需求标签
        labels = [g["label"] for g in r.payload["groups"]]
        assert labels == ["防晒", "衣服"]
        # 跨组去重：同一组商品不会重复进第二组（stub 每次返回相同 hits）
        all_ids = [p["product_id"] for p in r.payload["products"]]
        assert len(all_ids) == len(set(all_ids))  # 无重复
        # summary 反映多需求
        assert r.payload["summary"]["group_count"] >= 1
        assert r.payload["debug"]["multi_intent"] is True
        # composer_hint 引导分组介绍
        assert "分组" in r.composer_hint
        # last_hits 存合并后的全部命中
        assert session.get("last_hits")



# ---------- AnswerComposer ----------

class TestComposerBlocking:
    def test_override_short_circuits(self):
        sr = ToolResult(
            tool_name="clarify",
            narrative_override="请补充信息",
            needs_composer=False,
        )
        # 即使 composer 内部 client 没 mock 也不会被调用
        out = AnswerComposer().compose(sr, AgentSession())
        assert out == "请补充信息"

    def test_compose_calls_llm(self):
        sr = ToolResult(
            tool_name="recommend",
            payload={
                "query": "精华",
                "parsed": {"category": "美妆护肤", "max_price": 500},
                "hits": [{"title": "X 精华", "brand": "X", "base_price": 300, "score": 0.9}],
            },
            composer_hint="正常推荐",
        )
        fake_resp = _fake_chat_response("为你推荐：X 精华，性价比高。")
        with patch("agent.composer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = fake_resp
            out = AnswerComposer().compose(sr, AgentSession())
        assert "X 精华" in out

    def test_compose_extracts_json_object_from_wrapped_text(self):
        sr = ToolResult(
            tool_name="recommend",
            payload={"query": "精华", "hits": []},
            composer_hint="正常推荐",
        )
        wrapped = '好的，下面是推荐：\n{"opening":"为你推荐了几款","items":[],"followup":["推荐一些更平价的选择"]}\n希望有帮助。'
        fake_resp = _fake_chat_response(wrapped)
        with patch("agent.composer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = fake_resp
            out = AnswerComposer().compose(sr, AgentSession())
        assert out.startswith("{")
        assert out.endswith("}")
        assert "好的" not in out

    def test_compose_trim_drops_evidence(self):
        from agent.composer import _trim_payload_for_llm
        payload = {
            "query": "q",
            "parsed": {"category": "美妆护肤", "max_price": 500, "soft_terms": ["保湿"]},
            "hits": [{
                "title": "T", "brand": "B", "category": "c", "sub_category": "sc",
                "base_price": 100, "score": 0.8,
                "evidence": ["a" * 10000],
                "product_id": "p_beauty_001",
            }],
        }
        out = _trim_payload_for_llm(payload)
        assert "evidence" not in out["hits"][0]
        assert out["hits"][0]["productId"] == "p_beauty_001"
        assert "product_id" not in out["hits"][0]
        assert "soft_terms" not in out["parsed"]


class TestComposerStreaming:
    def test_stream_override_yields_once(self):
        sr = ToolResult(
            tool_name="clarify",
            narrative_override="请补充信息",
            needs_composer=False,
        )
        chunks = list(AnswerComposer().compose_stream(sr, AgentSession()))
        assert chunks == ["请补充信息"]

    def test_stream_no_composer_yields_nothing(self):
        sr = ToolResult(
            tool_name="fallback",
            narrative_override=None,
            needs_composer=False,
        )
        chunks = list(AnswerComposer().compose_stream(sr, AgentSession()))
        assert chunks == []

    def test_stream_yields_chunks_from_llm(self):
        sr = ToolResult(
            tool_name="recommend",
            payload={"query": "x", "parsed": {}, "hits": [{"title": "T", "base_price": 1}]},
            composer_hint="h",
        )
        with patch("agent.composer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = _fake_stream_chunks(
                ["你好", "，我", "推荐 T。"]
            )
            chunks = list(AnswerComposer().compose_stream(sr, AgentSession()))
        assert chunks == ["你好", "，我", "推荐 T。"]
        assert "".join(chunks) == "你好，我推荐 T。"

    def test_stream_empty_emits_placeholder(self):
        sr = ToolResult(
            tool_name="recommend",
            payload={"query": "x"},
            composer_hint="h",
        )
        with patch("agent.composer.get_client") as mock_client:
            # 空 chunks（极端情况）
            mock_client.return_value.chat.completions.create.return_value = iter([])
            chunks = list(AnswerComposer().compose_stream(sr, AgentSession()))
        assert len(chunks) == 1
        assert "放宽预算" in chunks[0]

    def test_stream_mid_flight_error_yields_tail(self):
        sr = ToolResult(
            tool_name="recommend",
            payload={"query": "x"},
            composer_hint="h",
        )

        def _gen():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="头一段"))])
            raise RuntimeError("network broken")

        with patch("agent.composer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = _gen()
            chunks = list(AnswerComposer().compose_stream(sr, AgentSession()))
        assert len(chunks) == 2
        assert chunks[0] == "头一段"
        assert "放宽预算" in chunks[1]
        assert "生成中断" not in chunks[0]

    def test_stream_handles_chunks_without_content(self):
        """有些 chunk 只是 role/finish 信号，没 content；不能崩。"""
        sr = ToolResult(
            tool_name="recommend",
            payload={"query": "x"},
            composer_hint="h",
        )
        chunks_in = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="a"))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace())]),  # 无 content 属性
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="b"))]),
        ]
        with patch("agent.composer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = iter(chunks_in)
            chunks = list(AnswerComposer().compose_stream(sr, AgentSession()))
        assert chunks == ["a", "b"]

    def test_build_messages_includes_payload_and_hint(self):
        from agent.composer import _build_messages

        sr = ToolResult(
            tool_name="recommend",
            payload={"query": "x", "hits": [{"product_id": "p1", "title": "T"}]},
            composer_hint="h",
        )
        messages = _build_messages(sr)
        assert messages[-1]["role"] == "user"
        assert "payload:" in messages[-1]["content"]
        assert "hint: h" in messages[-1]["content"]

    def test_product_detail_build_messages_uses_detail_prompt_and_evidence(self):
        from agent.composer import _build_messages

        sr = ToolResult(
            tool_name="product_detail",
            payload={
                "query": "这款评价怎么样",
                "focus_aspect": "reviews",
                "product": {"product_id": "p1", "title": "T"},
                "evidence": [{"source_type": "user_review", "text": "评价很好"}],
            },
            composer_hint="基于评价回答",
        )
        messages = _build_messages(sr)
        assert "单品导购问答" in messages[0]["content"]
        assert "不要输出 JSON" in messages[0]["content"]
        assert "✨ 总结" in messages[0]["content"]
        assert "🌟 优点" in messages[0]["content"]
        assert "🔍 缺点" in messages[0]["content"]
        assert "每段不超过 70 个中文字符" in messages[0]["content"]
        assert "2-3 个核心点" in messages[0]["content"]
        assert "评价很好" in messages[-1]["content"]
        assert "items" not in messages[0]["content"]

    def test_product_detail_prompt_includes_general_single_paragraph_format(self):
        from agent.composer import _build_messages

        sr = ToolResult(
            tool_name="product_detail",
            payload={
                "query": "这款怎么样",
                "focus_aspect": "general",
                "product": {"product_id": "p1", "title": "T"},
                "evidence": [{"source_type": "product_profile", "text": "降噪强，续航长"}],
            },
            composer_hint="基于详情回答",
        )
        messages = _build_messages(sr)
        assert "1 个自然段" in messages[0]["content"]
        assert "不要分点" in messages[0]["content"]
        assert "不要 emoji" in messages[0]["content"]
        assert "90 个中文字符" in messages[0]["content"]
        assert "禁止写成长段参数介绍" in messages[0]["content"]


# ---------- Orchestrator: 主流程 + 降级（非流式） ----------

class TestOrchestratorBlocking:
    def _patch_llm(self, router_resp, composer_text="OK"):
        router_client = MagicMock()
        router_client.chat.completions.create.return_value = router_resp
        composer_client = MagicMock()
        composer_client.chat.completions.create.return_value = _fake_chat_response(composer_text)
        return router_client, composer_client

    def test_happy_path_recommend(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "X", "brand": "X", "base_price": 200}
        ])
        router_resp = _fake_tool_response("recommend", "500内精华")
        rc, cc = self._patch_llm(router_resp, "为你推荐 X")
        with patch("agent.intent_router.get_client", return_value=rc), \
             patch("agent.composer.get_client", return_value=cc):
            resp = agent.handle_turn("500以内的精华", AgentSession())
        assert resp.decision.tool == "recommend"
        assert "X" in resp.narrative
        assert resp.trace["timings"]["router_ms"] >= 0
        assert resp.trace["timings"]["tool_ms"] >= 0

    def test_clarify_path(self):
        agent = _make_agent()
        router_resp = _fake_tool_response("clarify", "随便看看")
        rc, _ = self._patch_llm(router_resp)
        with patch("agent.intent_router.get_client", return_value=rc):
            resp = agent.handle_turn("随便看看", AgentSession())
        assert resp.decision.tool == "clarify"
        assert "品类" in resp.narrative

    def test_fallback_path(self):
        agent = _make_agent()
        router_resp = _fake_tool_response("fallback", "天气怎么样")
        rc, _ = self._patch_llm(router_resp)
        with patch("agent.intent_router.get_client", return_value=rc):
            resp = agent.handle_turn("今天天气怎么样", AgentSession())
        assert resp.decision.tool == "fallback"
        assert "导购" in resp.narrative

    def test_compare_guard_overrides_recommend_misroute(self):
        details = [
            _detail("p1", "Apple iPhone 17 Pro", "Apple 苹果"),
            _detail("p2", "小米 17 Ultra", "小米"),
        ]
        recommend = RecommendTool(
            search_service=_StubSearchService(hits=[
                {"product_id": "p1", "title": "Apple iPhone 17 Pro", "brand": "Apple 苹果", "base_price": 8999},
                {"product_id": "p2", "title": "小米 17 Ultra", "brand": "小米", "base_price": 7499},
            ]),
            decomposer=_single_decomposer,
        )
        agent = Agent(tools={
            "recommend": recommend,
            "refine": RefineTool(recommend=recommend),
            "compare": CompareTool(product_store=_FakeProductStore(details)),
            "product_detail": ProductDetailTool(),
            "cart": CartTool(),
            "clarify": ClarifyTool(),
            "fallback": FallbackTool(),
        })
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "Apple iPhone 17 Pro"},
            {"product_id": "p2", "title": "小米 17 Ultra"},
        ])
        router_resp = _fake_tool_response("recommend", "对比 Apple 和小米这两款")
        rc, _ = self._patch_llm(router_resp)
        with patch("agent.intent_router.get_client", return_value=rc), \
             patch("agent.tools.compare.build_comparison", side_effect=_fake_comparison):
            resp = agent.handle_turn("对比 Apple 和小米这两款", session)
        assert resp.decision.tool == "compare"
        assert resp.tool_result.payload["comparison"] is not None

    def test_attribute_followup_with_named_product_routes_to_product_detail(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "Apple iPhone 17 Pro"},
            {"product_id": "p2", "title": "小米 17 Ultra"},
        ])
        router_decision = IntentDecision(
            tool="recommend",
            rewritten_query="推荐续航表现优秀的小米手机",
            confidence="high",
            reasoning="router misroute",
        )

        with patch("agent.orchestrator.route", return_value=router_decision):
            decision = agent._safe_route("小米的续航怎么样？", session, {"timings": {}})

        assert decision.tool == "product_detail"
        assert decision.rewritten_query == "小米的续航怎么样？"

    def test_router_injected_compare_word_does_not_force_compare(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "小米 17 Max 大屏长续航"},
            {"product_id": "p2", "title": "小米 17 Ultra 影像手机"},
        ])
        router_decision = IntentDecision(
            tool="recommend",
            rewritten_query="对比刚才推荐的两款小米手机的续航表现",
            confidence="high",
            reasoning="router injected compare wording",
        )

        with patch("agent.orchestrator.route", return_value=router_decision):
            decision = agent._safe_route("小米的续航怎么样？", session, {"timings": {}})

        assert decision.tool == "product_detail"
        assert decision.rewritten_query == "小米的续航怎么样？"

    def test_group_attribute_followup_routes_to_compare(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "Apple iPad Pro"},
            {"product_id": "p2", "title": "小米平板 8 Pro"},
        ])
        router_decision = IntentDecision(
            tool="recommend",
            rewritten_query="推荐续航强的平板",
            confidence="high",
            reasoning="router misroute",
        )

        with patch("agent.orchestrator.route", return_value=router_decision):
            decision = agent._safe_route("这几款平板哪个续航更好？", session, {"timings": {}})

        assert decision.tool == "compare"

    def test_new_attribute_search_stays_recommend(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "Apple iPad Pro"},
            {"product_id": "p2", "title": "小米平板 8 Pro"},
        ])
        router_decision = IntentDecision(
            tool="recommend",
            rewritten_query="推荐续航好的平板",
            confidence="high",
            reasoning="fresh search",
        )

        with patch("agent.orchestrator.route", return_value=router_decision):
            decision = agent._safe_route("推荐续航好的平板", session, {"timings": {}})

        assert decision.tool == "recommend"

    def test_bare_attribute_followup_with_focus_routes_to_product_detail(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "Apple iPad Pro"},
            {"product_id": "p2", "title": "小米平板 8 Pro"},
        ])
        session.set("last_focus_product_id", "p2")
        router_decision = IntentDecision(
            tool="recommend",
            rewritten_query="推荐续航好的平板",
            confidence="high",
            reasoning="router misroute",
        )

        with patch("agent.orchestrator.route", return_value=router_decision):
            decision = agent._safe_route("续航怎么样？", session, {"timings": {}})

        assert decision.tool == "product_detail"
        assert decision.rewritten_query == "续航怎么样？"

    def test_pending_detail_selection_forces_product_detail_without_router(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "Apple iPad Pro"},
            {"product_id": "p2", "title": "小米平板 8 Pro"},
        ])
        session.set("pending_detail", {
            "candidates": [{"product_id": "p1", "title": "Apple"}, {"product_id": "p2", "title": "小米"}],
            "focus_query": "续航怎么样",
        })

        # 不打 patch route——若被调用会抛错；这里应跳过 router 直接 product_detail。
        decision = agent._safe_route("第一款", session, {"timings": {}})
        assert decision.tool == "product_detail"
        assert decision.rewritten_query == "第一款"

    def test_pending_detail_topic_change_releases_and_routes_normally(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [{"product_id": "p1", "title": "Apple iPad Pro"}])
        session.set("pending_detail", {"candidates": [{"product_id": "p1", "title": "A"}], "focus_query": "x"})
        router_decision = IntentDecision(
            tool="recommend", rewritten_query="推荐几款手机", confidence="high", reasoning="fresh",
        )

        with patch("agent.orchestrator.route", return_value=router_decision):
            decision = agent._safe_route("推荐几款手机", session, {"timings": {}})

        assert decision.tool == "recommend"
        assert session.get("pending_detail") is None

    def test_pending_compare_selection_forces_compare_without_router(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "p1", "title": "Apple iPhone"},
            {"product_id": "p2", "title": "小米"},
            {"product_id": "p3", "title": "华为"},
        ])
        session.set("pending_compare", {"hit_count": 3})

        decision = agent._safe_route("第一个和第三个", session, {"timings": {}})
        assert decision.tool == "compare"
        assert decision.rewritten_query == "第一个和第三个"

    def test_pending_cart_holds_for_spec_reply(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("pending_cart", {"product_id": "p1", "title": "x", "quantity": 1, "spec_text": ""})

        # “暗夜黑 42码”是规格应答，必须留在 cart 完成加购。
        decision = agent._safe_route("暗夜黑 42码", session, {"timings": {}})
        assert decision.tool == "cart"
        assert session.get("pending_cart") is not None

    def test_pending_cart_releases_on_new_search_escape(self):
        agent = _make_agent()
        session = AgentSession()
        session.set("pending_cart", {"product_id": "p1", "title": "x", "quantity": 1, "spec_text": ""})
        router_decision = IntentDecision(
            tool="recommend", rewritten_query="推荐几款笔记本", confidence="high", reasoning="fresh",
        )

        # 用户中途改口开新检索 → 释放 pending，交回正常路由。
        with patch("agent.orchestrator.route", return_value=router_decision):
            decision = agent._safe_route("推荐几款笔记本", session, {"timings": {}})
        assert decision.tool == "recommend"
        assert session.get("pending_cart") is None

    def test_new_search_escape_detection(self):
        from agent.orchestrator import _is_new_search_escape

        assert _is_new_search_escape("推荐几款笔记本") is True
        assert _is_new_search_escape("有没有便宜点的平板") is True
        assert _is_new_search_escape("换成华为的看看") is True
        # 规格应答 / 聚焦指代不算逃逸
        assert _is_new_search_escape("暗夜黑 42码") is False
        assert _is_new_search_escape("这个加进来") is False
        assert _is_new_search_escape("第一个") is False

    def test_compare_uses_raw_query_for_first_and_last_reference(self):
        details = [
            _detail("hoodie", "李宁 运动生活系列 男子连帽套头卫衣", "李宁"),
            _detail("shorts", "优衣库 男装 DRY 速干运动短裤", "优衣库"),
            _detail("pants", "Nike Dri-FIT 男子训练长裤", "Nike"),
            _detail("tee", "优衣库 DRY-EX 超快干圆领短袖T恤", "优衣库"),
        ]
        agent = Agent(tools={
            "recommend": RecommendTool(search_service=_StubSearchService(), decomposer=_single_decomposer),
            "refine": RefineTool(),
            "compare": CompareTool(product_store=_FakeProductStore(details)),
            "product_detail": ProductDetailTool(),
            "cart": CartTool(),
            "clarify": ClarifyTool(),
            "fallback": FallbackTool(),
        })
        session = AgentSession()
        session.set("last_hits", [
            {"product_id": "hoodie", "title": "李宁 运动生活系列 男子连帽套头卫衣"},
            {"product_id": "shorts", "title": "优衣库 男装 DRY 速干运动短裤"},
            {"product_id": "pants", "title": "Nike Dri-FIT 男子训练长裤"},
            {"product_id": "tee", "title": "优衣库 DRY-EX 超快干圆领短袖T恤"},
        ])
        # 模拟 router 把“第一个”错误改写成短裤；工具仍应使用 raw query 的首尾指代。
        router_resp = _fake_tool_response("compare", "对比优衣库短裤和优衣库T恤")
        rc, _ = self._patch_llm(router_resp)
        with patch("agent.intent_router.get_client", return_value=rc), \
             patch("agent.tools.compare.build_comparison", side_effect=_fake_comparison):
            resp = agent.handle_turn("对比一下第一个和最后一个", session)
        ids = [p["product_id"] for p in resp.tool_result.payload["comparison"]["products"]]
        assert ids == ["hoodie", "tee"]


# ---------- Orchestrator: edge cases（非流式） ----------

class TestOrchestratorEdgeCases:
    def test_empty_input_shortcut(self):
        agent = _make_agent()
        with patch("agent.intent_router.get_client") as mock_router_client, \
             patch("agent.composer.get_client") as mock_composer_client:
            resp = agent.handle_turn("", AgentSession())
            mock_router_client.assert_not_called()
            mock_composer_client.assert_not_called()
        assert resp.decision.tool == "clarify"
        assert "品类" in resp.narrative

    def test_whitespace_only_input(self):
        agent = _make_agent()
        resp = agent.handle_turn("   \t\n  ", AgentSession())
        assert resp.decision.tool == "clarify"

    def test_router_failure_defaults_to_recommend(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "兜底商品", "brand": "X", "base_price": 100}
        ])
        with patch("agent.orchestrator.route", side_effect=RuntimeError("ark down")), \
             patch("agent.composer.get_client") as mock_cc:
            mock_cc.return_value.chat.completions.create.return_value = _fake_chat_response("兜底回答")
            resp = agent.handle_turn("精华", AgentSession())
        assert resp.decision.tool == "recommend"
        assert resp.decision.confidence == "low"
        assert "router_error" in resp.trace
        assert "兜底" in resp.narrative

    def test_tool_failure_degrades_to_apology(self):
        class _BoomTool:
            name = "recommend"
            def run(self, *a, **kw):
                raise RuntimeError("search index corrupt")

        agent = Agent(tools={
            "recommend": _BoomTool(),
            "refine": RefineTool(),
            "compare": CompareTool(),
            "product_detail": ProductDetailTool(),
            "cart": CartTool(),
            "clarify": ClarifyTool(),
            "fallback": FallbackTool(),
        })
        router_resp = _fake_tool_response("recommend", "精华")
        with patch("agent.intent_router.get_client") as mock_rc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            resp = agent.handle_turn("精华", AgentSession())
        assert "暂时无法处理" in resp.narrative
        assert "tool_error" in resp.trace

    def test_composer_failure_uses_fallback_text(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "雅诗兰黛精华", "brand": "雅诗兰黛", "base_price": 300},
            {"product_id": "p2", "title": "兰蔻小黑瓶", "brand": "兰蔻", "base_price": 600},
        ])
        router_resp = _fake_tool_response("recommend", "精华")
        with patch("agent.intent_router.get_client") as mock_rc, \
             patch("agent.composer.get_client") as mock_cc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            mock_cc.return_value.chat.completions.create.side_effect = RuntimeError("rate limit")
            resp = agent.handle_turn("精华", AgentSession())
        assert "雅诗兰黛精华" in resp.narrative or "兰蔻小黑瓶" in resp.narrative
        assert "composer_error" in resp.trace

    def test_composer_failure_zero_hits(self):
        agent = _make_agent(tool_search_hits=[])
        router_resp = _fake_tool_response("recommend", "x")
        with patch("agent.intent_router.get_client") as mock_rc, \
             patch("agent.composer.get_client") as mock_cc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            mock_cc.return_value.chat.completions.create.side_effect = RuntimeError("boom")
            resp = agent.handle_turn("奇怪东西", AgentSession())
        assert "没找到" in resp.narrative

    def test_router_returns_unknown_tool(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "X", "brand": "X", "base_price": 100}
        ])
        bad_resp = _fake_tool_response("super_recommend", "x")
        with patch("agent.intent_router.get_client") as mock_rc:
            mock_rc.return_value.chat.completions.create.return_value = bad_resp
            resp = agent.handle_turn("x", AgentSession())
        # 未知/漏填 tool 时退回 recommend（购物安全默认），不再误判成 clarify。
        assert resp.decision.tool == "recommend"

    def test_router_returns_no_tool_call(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "X", "brand": "X", "base_price": 100}
        ])
        bad_msg = SimpleNamespace(tool_calls=None, content="我觉得你应该...")
        bad_resp = SimpleNamespace(choices=[SimpleNamespace(message=bad_msg)])
        with patch("agent.intent_router.get_client") as mock_rc, \
             patch("agent.composer.get_client") as mock_cc:
            mock_rc.return_value.chat.completions.create.return_value = bad_resp
            mock_cc.return_value.chat.completions.create.return_value = _fake_chat_response("ok")
            resp = agent.handle_turn("精华", AgentSession())
        assert resp.decision.tool == "recommend"
        assert resp.decision.confidence == "low"

    def test_session_history_recorded(self):
        agent = _make_agent(tool_search_hits=[])
        router_resp = _fake_tool_response("fallback", "x")
        sess = AgentSession()
        with patch("agent.intent_router.get_client") as mock_rc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            agent.handle_turn("hi", sess)
        assert len(sess.history) == 2
        assert sess.history[0].role == "user"
        assert sess.history[1].role == "assistant"

    def test_multi_turn_context_passed_to_router(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "X", "brand": "X", "base_price": 100}
        ])
        router_resp = _fake_tool_response("recommend", "300以内精华")
        sess = AgentSession()
        sess.add_user("500以内的精华")
        sess.add_assistant("推荐 A B C")

        captured: dict[str, Any] = {}
        def _capture(model, messages, **kw):
            captured["messages"] = messages
            return router_resp

        with patch("agent.intent_router.get_client") as mock_rc, \
             patch("agent.composer.get_client") as mock_cc:
            mock_rc.return_value.chat.completions.create.side_effect = _capture
            mock_cc.return_value.chat.completions.create.return_value = _fake_chat_response("ok")
            agent.handle_turn("再便宜点", sess)

        user_msg = captured["messages"][-1]["content"]
        assert "最近对话" in user_msg
        assert "500以内的精华" in user_msg

    def test_missing_tool_implementation_raises(self):
        with pytest.raises(RuntimeError, match="Missing tool"):
            Agent(tools={"recommend": RecommendTool()})  # 缺 clarify / fallback


# ---------- Orchestrator: 流式 ----------

class TestOrchestratorStreaming:
    def test_stream_happy_event_order(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "X", "brand": "X", "base_price": 200}
        ])
        router_resp = _fake_tool_response("recommend", "精华")
        with patch("agent.intent_router.get_client") as mock_rc, \
             patch("agent.composer.get_client") as mock_cc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            mock_cc.return_value.chat.completions.create.return_value = _fake_stream_chunks(
                ["为你", "推荐 ", "X"]
            )
            events = list(agent.handle_turn_stream("精华", AgentSession()))
        types = [e["type"] for e in events]
        # 过滤掉 status 后断言核心管线顺序：meta → tool_result → token+ → done
        core = [t for t in types if t != "status"]
        assert core[0] == "meta"
        assert core[1] == "tool_result"
        assert core[-1] == "done"
        # 至少出现一个 status（路由阶段）
        assert "status" in types
        tokens = [e["data"] for e in events if e["type"] == "token"]
        assert "".join(tokens) == "为你推荐 X"
        # done 携带 timings + narrative
        done_data = events[-1]["data"]
        assert done_data["narrative"] == "为你推荐 X"
        assert "router_ms" in done_data["timings"]
        assert "tool_ms" in done_data["timings"]
        assert "composer_ms" in done_data["timings"]

    def test_stream_empty_input(self):
        """空输入也能正常走完流式事件序列。"""
        agent = _make_agent()
        # 不应触发任何 LLM 调用
        with patch("agent.intent_router.get_client") as mock_rc, \
             patch("agent.composer.get_client") as mock_cc:
            events = list(agent.handle_turn_stream("", AgentSession()))
            mock_rc.assert_not_called()
            mock_cc.assert_not_called()
        types = [e["type"] for e in events]
        # 空输入快捷路径不发 status
        assert types == ["meta", "tool_result", "token", "done"]
        assert "品类" in events[2]["data"]

    def test_stream_clarify_path_uses_override(self):
        """clarify tool 用 narrative_override，流式应只有一个 token 事件。"""
        agent = _make_agent()
        router_resp = _fake_tool_response("clarify", "随便")
        with patch("agent.intent_router.get_client") as mock_rc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            events = list(agent.handle_turn_stream("随便", AgentSession()))
        tokens = [e for e in events if e["type"] == "token"]
        assert len(tokens) == 1
        assert "品类" in tokens[0]["data"]

    def test_stream_router_failure_still_completes(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "Z", "brand": "Z", "base_price": 50}
        ])
        with patch("agent.orchestrator.route", side_effect=RuntimeError("ark down")), \
             patch("agent.composer.get_client") as mock_cc:
            mock_cc.return_value.chat.completions.create.return_value = _fake_stream_chunks(["兜底"])
            events = list(agent.handle_turn_stream("精华", AgentSession()))
        meta = next(e for e in events if e["type"] == "meta")
        assert meta["data"]["decision"]["tool"] == "recommend"
        assert meta["data"]["decision"]["confidence"] == "low"
        assert events[-1]["type"] == "done"

    def test_stream_tool_failure_yields_apology(self):
        class _BoomTool:
            name = "recommend"
            def run(self, *a, **kw):
                raise RuntimeError("boom")

        agent = Agent(tools={
            "recommend": _BoomTool(),
            "refine": RefineTool(),
            "compare": CompareTool(),
            "product_detail": ProductDetailTool(),
            "cart": CartTool(),
            "clarify": ClarifyTool(),
            "fallback": FallbackTool(),
        })
        router_resp = _fake_tool_response("recommend", "精华")
        with patch("agent.intent_router.get_client") as mock_rc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            events = list(agent.handle_turn_stream("精华", AgentSession()))
        tokens = "".join(e["data"] for e in events if e["type"] == "token")
        assert "暂时无法处理" in tokens

    def test_stream_records_session_history(self):
        agent = _make_agent(tool_search_hits=[
            {"product_id": "p1", "title": "X", "brand": "X", "base_price": 1}
        ])
        router_resp = _fake_tool_response("recommend", "x")
        sess = AgentSession()
        with patch("agent.intent_router.get_client") as mock_rc, \
             patch("agent.composer.get_client") as mock_cc:
            mock_rc.return_value.chat.completions.create.return_value = router_resp
            mock_cc.return_value.chat.completions.create.return_value = _fake_stream_chunks(["回答"])
            list(agent.handle_turn_stream("hi", sess))
        assert len(sess.history) == 2
        assert sess.history[-1].content == "回答"
