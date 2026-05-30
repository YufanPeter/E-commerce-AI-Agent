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
from agent.tools.compare import CompareTool
from agent.tools.fallback import FallbackTool
from agent.tools.product_detail import ProductDetailTool
from agent.tools.recommend import RecommendTool
from agent.tools.refine import RefineTool


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


class _StubSearchService:
    """模拟 SearchService，可控返回 hits 和 needs_clarification。"""

    def __init__(self, hits: list[dict] | None = None, needs_clarify: bool = False):
        self._hits = hits or []
        self._needs_clarify = needs_clarify

    def search(self, query: str, top_k_chunks: int = 50, top_k_products: int = 10, base=None):
        parsed = SimpleNamespace(
            needs_clarification=self._needs_clarify,
            category="美妆护肤",
            max_price=500,
            to_dict=lambda: {"category": "美妆护肤", "max_price": 500, "needs_clarification": self._needs_clarify},
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


def _make_agent(
    tool_search_hits: list[dict] | None = None,
    needs_clarify: bool = False,
) -> Agent:
    stub = _StubSearchService(hits=tool_search_hits, needs_clarify=needs_clarify)
    return Agent(tools={
        "recommend": RecommendTool(search_service=stub),
        "refine": RefineTool(),
        "compare": CompareTool(),
        "product_detail": ProductDetailTool(),
        "clarify": ClarifyTool(),
        "fallback": FallbackTool(),
    })


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
        tool = RecommendTool(search_service=_StubSearchService(hits=hits))
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
        tool = RecommendTool(search_service=_StubSearchService(hits=[]))
        r = tool.run("xxx", AgentSession(), {})
        assert "未命中" in r.composer_hint

    def test_needs_clarification_hint(self):
        tool = RecommendTool(search_service=_StubSearchService(hits=[], needs_clarify=True))
        r = tool.run("随便看看", AgentSession(), {})
        assert "模糊" in r.composer_hint


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

    def test_compose_trim_drops_evidence(self):
        from agent.composer import _trim_payload_for_llm
        payload = {
            "query": "q",
            "parsed": {"category": "美妆护肤", "max_price": 500, "soft_terms": ["保湿"]},
            "hits": [{
                "title": "T", "brand": "B", "category": "c", "sub_category": "sc",
                "base_price": 100, "score": 0.8,
                "evidence": ["a" * 10000],
                "product_id": "should_drop",
            }],
        }
        out = _trim_payload_for_llm(payload)
        assert "evidence" not in out["hits"][0]
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
        assert chunks == ["（暂未生成内容，请换种说法再试一次）"]

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
        # 应该至少有头部 + 兜底提示
        assert chunks[0] == "头一段"
        assert any("生成中断" in c for c in chunks)

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
        assert "".join(chunks) == "ab"


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
        agent = _make_agent()
        bad_resp = _fake_tool_response("super_recommend", "x")
        with patch("agent.intent_router.get_client") as mock_rc:
            mock_rc.return_value.chat.completions.create.return_value = bad_resp
            resp = agent.handle_turn("x", AgentSession())
        assert resp.decision.tool == "clarify"

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
