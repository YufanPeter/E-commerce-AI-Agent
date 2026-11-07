"""流式事件顺序契约：开场白先到、商品解说逐款到、追问最后，再补兼容 token。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.intent_router import clear_route_cache
from agent.orchestrator import Agent
from agent.session import AgentSession
from agent.tools.base import ToolResult


class _StubTool:
    name = "recommend"

    def run(self, query, session, slots):
        return ToolResult(tool_name="recommend", payload={"query": query, "products": [
            {"product_id": "p1", "title": "A"}, {"product_id": "p2", "title": "B"},
        ]}, composer_hint="h")


class _Static:
    name = "x"

    def __init__(self, name):
        self.name = name

    def run(self, query, session, slots):
        return ToolResult(tool_name=self.name, payload={}, narrative_override="固定话术", needs_composer=False)


def _agent():
    tools = {n: _Static(n) for n in ("refine", "compare", "product_detail", "cart", "clarify", "fallback")}
    tools["recommend"] = _StubTool()
    return Agent(tools=tools)


def _router_resp(tool="recommend"):
    args = json.dumps({"tool": tool, "rewritten_query": "耳机", "confidence": "high", "reasoning": ""})
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=args))], content=None))])


def _chunks(pieces):
    for p in pieces:
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=p))])


def test_stream_emits_structured_parts_in_order_then_compat_token():
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = _chunks(
            ["开场｜为你", "找到两款\n商品｜p1｜轻\n", "商品｜p2｜稳\n追问｜便宜点"]
        )
        events = list(_agent().handle_turn_stream("耳机", AgentSession()))
    types = [e["type"] for e in events]
    assert types[:5] == ["status", "meta", "status", "tool_result", "status"]
    narrative = [e["data"] for e in events if e["type"] == "narrative"]
    assert [n["part"] for n in narrative] == ["opening", "opening", "opening_end", "item", "item", "followup"]
    # 商品解说按商品顺序到达
    assert [n.get("productId") for n in narrative if n["part"] == "item"] == ["p1", "p2"]
    # 结构化片段之后仍有一条完整 JSON token（兼容旧客户端），最后 done
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) == 1 and tokens[0]["data"].startswith("{")
    assert types[-1] == "done"
    assert events.index(tokens[0]) > max(i for i, e in enumerate(events) if e["type"] == "narrative")
    assert json.loads(events[-1]["data"]["narrative"])["items"][1]["productId"] == "p2"


def test_first_turn_router_result_is_cached():
    clear_route_cache()
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.side_effect = lambda **kw: _chunks(["开场｜o\n"])
        list(_agent().handle_turn_stream("耳机", AgentSession()))
        list(_agent().handle_turn_stream("耳机", AgentSession()))
        assert rc.return_value.chat.completions.create.call_count == 1
        # 有历史的会话不走缓存
        sess = AgentSession()
        sess.add_user("之前的话")
        sess.add_assistant("之前的回答")
        list(_agent().handle_turn_stream("耳机", sess))
        assert rc.return_value.chat.completions.create.call_count == 2
