"""用户中断流式回答：上游 LLM 流要关闭，已发出的内容要记入会话。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.orchestrator import Agent, record_abandoned_turn
from agent.session import AgentSession
from agent.tools.base import ToolResult
from llm.client import _ChainChatCompletions


class _TrackedStream:
    """模拟 OpenAI Stream：记录是否被关闭。"""

    def __init__(self, pieces):
        self.pieces = pieces
        self.closed = False

    def __iter__(self):
        for piece in self.pieces:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])

    def close(self):
        self.closed = True


class _RecommendStub:
    name = "recommend"

    def run(self, query, session, slots):
        session.remember_search(
            {"query": query},
            [{"product_id": "p1", "title": "A"}, {"product_id": "p2", "title": "B"}],
        )
        return ToolResult(
            tool_name="recommend",
            payload={"query": query, "products": [{"product_id": "p1"}, {"product_id": "p2"}]},
            composer_hint="h",
        )


class _DetailStub:
    name = "product_detail"

    def run(self, query, session, slots):
        return ToolResult(tool_name="product_detail", payload={"query": query}, composer_hint="h")


class _Static:
    def __init__(self, name):
        self.name = name

    def run(self, query, session, slots):
        return ToolResult(tool_name=self.name, payload={}, narrative_override="固定话术", needs_composer=False)


def _agent():
    tools = {n: _Static(n) for n in ("refine", "compare", "cart", "clarify", "fallback")}
    tools["recommend"] = _RecommendStub()
    tools["product_detail"] = _DetailStub()
    return Agent(tools=tools)


def _router_resp(tool="recommend"):
    args = json.dumps({"tool": tool, "rewritten_query": "耳机", "confidence": "high", "reasoning": ""})
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=args))], content=None))])


def _run_until(stream, stop):
    """消费事件直到 stop(event) 为真，然后像客户端断连那样关闭生成器。"""
    for event in stream:
        if stop(event):
            break
    stream.close()


def _is_item(event):
    return event["type"] == "narrative" and event["data"].get("part") == "item"


def test_close_after_first_card_keeps_only_what_was_shown():
    upstream = _TrackedStream(["开场｜为你找到两款\n", "商品｜p2｜轻\n", "商品｜p1｜稳\n", "追问｜便宜点\n"])
    session = AgentSession()
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = upstream
        _run_until(_agent().handle_turn_stream("耳机", session), _is_item)

    assert upstream.closed
    assert [m.role for m in session.history] == ["user", "assistant"]
    saved = json.loads(session.history[-1].content)
    assert saved["interrupted"] is True
    assert saved["opening"] == "为你找到两款"
    assert [i["productId"] for i in saved["items"]] == ["p2"]
    # "第一个"要指向用户看到的 p2，而不是检索排序里的 p1
    assert [h["product_id"] for h in session.recall_hits()] == ["p2"]
    # 上一轮结构化意图保留，用户接着说"便宜点"还能叠加
    assert session.recall_parsed() == {"query": "耳机"}


def test_close_before_any_card_restores_previous_hits():
    upstream = _TrackedStream(["开场｜为你找到两款\n", "商品｜p1｜轻\n"])
    session = AgentSession()
    previous = [{"product_id": "old", "title": "上一轮"}]
    session.task.show("search", previous)
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = upstream
        _run_until(_agent().handle_turn_stream("耳机", session), lambda e: e["type"] == "tool_result")

    assert session.recall_hits() == previous
    assert "中断" in session.history[-1].content
    assert session.history[-1].role == "assistant"


def test_close_during_plain_text_answer_keeps_partial_text():
    upstream = _TrackedStream(["这款", "耳机", "续航很长"])
    session = AgentSession()
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp("product_detail")
        cc.return_value.chat.completions.create.return_value = upstream
        tokens = []

        def stop(event):
            if event["type"] == "token":
                tokens.append(event["data"])
            return len(tokens) == 2

        _run_until(_agent().handle_turn_stream("续航怎么样", session), stop)

    assert upstream.closed
    assert session.history[-1].content.startswith("这款耳机")
    assert session.history[-1].content.endswith("（回答被用户中断）")


def test_closing_after_completed_turn_does_not_add_a_second_answer():
    upstream = _TrackedStream(["开场｜好\n", "商品｜p1｜轻\n"])
    session = AgentSession()
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = upstream
        _run_until(_agent().handle_turn_stream("耳机", session), lambda e: e["type"] == "done")

    assert [m.role for m in session.history] == ["user", "assistant"]
    assert "interrupted" not in session.history[-1].content
    assert upstream.closed


def test_interrupted_answer_is_not_cached():
    first = _TrackedStream(["开场｜半截\n", "商品｜p1｜轻\n", "商品｜p2｜稳\n"])
    second = _TrackedStream(["开场｜完整\n", "商品｜p1｜轻\n", "商品｜p2｜稳\n"])
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.side_effect = [first, second]
        _run_until(_agent().handle_turn_stream("耳机", AgentSession()), _is_item)
        events = list(_agent().handle_turn_stream("耳机", AgentSession()))

    openings = [e["data"]["text"] for e in events
                if e["type"] == "narrative" and e["data"].get("part") == "opening_end"]
    assert openings == ["完整"]


def test_provider_chain_closes_upstream_stream_when_consumer_stops():
    upstream = _TrackedStream(["a", "b"])
    backend = SimpleNamespace(
        name="primary",
        breaker=SimpleNamespace(record_success=lambda: None, record_failure=lambda exc: None),
    )
    chain = _ChainChatCompletions.__new__(_ChainChatCompletions)
    wrapped = chain._stream_with_fallback(upstream, backend, [], {})
    next(wrapped)
    wrapped.close()
    assert upstream.closed


def test_completed_turn_orders_references_like_the_screen():
    upstream = _TrackedStream(["开场｜好\n", "分组｜x\n", "商品｜p2｜轻\n", "分组｜y\n", "商品｜p1｜稳\n"])
    session = AgentSession()
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = upstream
        list(_agent().handle_turn_stream("耳机", session))

    assert [h["product_id"] for h in session.recall_hits()] == ["p2", "p1"]


def test_products_without_description_stay_last_like_the_screen():
    upstream = _TrackedStream(["开场｜好\n", "商品｜p2｜轻\n"])
    session = AgentSession()
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = upstream
        list(_agent().handle_turn_stream("耳机", session))

    assert [h["product_id"] for h in session.recall_hits()] == ["p2", "p1"]


def _interrupted_session(query="推荐跑鞋"):
    session = AgentSession()
    record_abandoned_turn(session, query)
    return session


def test_cancel_after_interrupt_gets_an_acknowledgement_without_routing():
    session = _interrupted_session()
    with patch("agent.intent_router.get_client") as rc:
        events = list(_agent().handle_turn_stream("算了，不要了", session))
    rc.return_value.chat.completions.create.assert_not_called()
    assert [e["type"] for e in events] == ["meta", "tool_result", "token", "done"]
    assert "先不看了" in events[-1]["data"]["narrative"]
    assert [m.role for m in session.history] == ["user", "assistant", "user", "assistant"]


def test_cancel_with_a_new_request_still_goes_to_the_router():
    session = _interrupted_session()
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = _TrackedStream(["开场｜好\n"])
        list(_agent().handle_turn_stream("算了，看看手机吧", session))
    rc.return_value.chat.completions.create.assert_called()


def test_same_words_without_an_interrupt_go_to_the_router():
    session = AgentSession()
    session.add_user("推荐跑鞋")
    session.add_assistant("为你挑了几款")
    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = _TrackedStream(["开场｜好\n"])
        list(_agent().handle_turn_stream("算了", session))
    rc.return_value.chat.completions.create.assert_called()
