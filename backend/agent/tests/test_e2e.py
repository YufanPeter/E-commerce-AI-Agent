"""E2E 测试：真实调 Ark + Chroma，验证 happy path（含流式）。

仅在 RUN_E2E=1 时执行；CI 默认 skip。
"""

from __future__ import annotations

import os

import pytest

from agent.orchestrator import Agent
from agent.session import AgentSession


_RUN = os.environ.get("RUN_E2E") == "1"
pytestmark = pytest.mark.skipif(not _RUN, reason="需要 RUN_E2E=1 与 ARK_API_KEY")


@pytest.fixture(scope="module")
def agent():
    return Agent()


class TestE2EBlocking:
    def test_recommend_happy(self, agent):
        sess = AgentSession()
        resp = agent.handle_turn("推荐 500 以内的敏感肌精华", sess)
        assert resp.decision.tool == "recommend"
        assert resp.tool_result.payload["products"], "应至少命中 1 个商品"
        assert resp.narrative

    def test_zero_hit_recommend(self, agent):
        sess = AgentSession()
        resp = agent.handle_turn("5000 以内非苹果的笔记本电脑", sess)
        assert resp.decision.tool == "recommend"
        assert resp.narrative

    def test_clarify_route(self, agent):
        sess = AgentSession()
        resp = agent.handle_turn("随便看看", sess)
        assert resp.decision.tool in ("clarify", "recommend")
        assert resp.narrative

    def test_fallback_route(self, agent):
        sess = AgentSession()
        resp = agent.handle_turn("今天上海天气怎么样", sess)
        assert resp.decision.tool == "fallback"
        assert "导购" in resp.narrative

    def test_multi_turn_refine(self, agent):
        sess = AgentSession()
        agent.handle_turn("推荐 500 以内的精华", sess)
        resp = agent.handle_turn("再便宜点", sess)
        assert resp.decision.tool == "recommend"
        assert resp.decision.rewritten_query


class TestE2EStreaming:
    def test_stream_recommend_emits_all_event_types(self, agent):
        sess = AgentSession()
        events = list(agent.handle_turn_stream("推荐 500 以内的防晒霜", sess))
        types = {e["type"] for e in events}
        assert "meta" in types
        assert "tool_result" in types
        assert "token" in types
        assert events[-1]["type"] == "done"
        narrative = events[-1]["data"]["narrative"]
        assert narrative
        # session 历史已写入
        assert sess.history[-1].content == narrative

    def test_stream_clarify_single_token(self, agent):
        sess = AgentSession()
        events = list(agent.handle_turn_stream("随便看看", sess))
        tokens = [e for e in events if e["type"] == "token"]
        # clarify 静态文本，只产 1 个 token；recommend 流式则多个。
        # 这里只断言总体流程能跑完。
        assert tokens
        assert events[-1]["type"] == "done"
