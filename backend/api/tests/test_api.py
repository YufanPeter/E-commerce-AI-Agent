"""FastAPI 端点测试：覆盖 /chat 与 /chat/stream 的 happy + edge case。

用 TestClient + mock Agent，避免真实 LLM 调用。
"""

from __future__ import annotations

from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    # 必须延迟 import，因为 api.main 顶层会 import agent，
    # 而 conftest / 其他测试可能已经把 client mock 过
    from api import main as api_main

    # 重置 agent 单例，避免上一个 test 的 mock 残留
    api_main._agent = None
    return TestClient(api_main.app), api_main


class _FakeAgent:
    """模拟 Agent：可控返回结构化事件。"""

    def __init__(self, events=None, response=None):
        self._events = events or []
        self._response = response

    def handle_turn(self, query, session):
        from agent.intent_router import IntentDecision
        from agent.tools.base import ToolResult
        from agent.orchestrator import AgentResponse
        if self._response is not None:
            return self._response
        return AgentResponse(
            decision=IntentDecision(tool="recommend", rewritten_query=query, confidence="high", reasoning="ok"),
            tool_result=ToolResult(tool_name="recommend", payload={"hits": []}),
            narrative=f"对 {query} 的回答",
            trace={"timings": {"router_ms": 1, "tool_ms": 1, "composer_ms": 1}},
        )

    def handle_turn_stream(self, query, session) -> Iterator[dict]:
        if self._events:
            yield from self._events
            return
        yield {"type": "meta", "data": {"decision": {"tool": "recommend", "rewritten_query": query, "confidence": "high", "reasoning": ""}, "trace": {"timings": {}}}}
        yield {"type": "tool_result", "data": {"tool_name": "recommend", "payload": {"hits": []}}}
        yield {"type": "token", "data": "hi "}
        yield {"type": "token", "data": query}
        yield {"type": "done", "data": {"timings": {"router_ms": 1, "tool_ms": 1, "composer_ms": 1}, "narrative": f"hi {query}"}}


class TestHealthz:
    def test_ok(self, client):
        c, _ = client
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestChatBlocking:
    def test_happy_path(self, client):
        c, m = client
        m._agent = _FakeAgent()
        r = c.post("/chat", json={"query": "推荐精华"})
        assert r.status_code == 200
        body = r.json()
        assert body["narrative"] == "对 推荐精华 的回答"
        assert body["decision"]["tool"] == "recommend"
        assert body["session_id"]

    def test_empty_query_rejected(self, client):
        c, m = client
        m._agent = _FakeAgent()
        r = c.post("/chat", json={"query": "   "})
        assert r.status_code == 400

    def test_session_persists_across_calls(self, client):
        c, m = client
        m._agent = _FakeAgent()
        r1 = c.post("/chat", json={"query": "first"})
        sid = r1.json()["session_id"]
        r2 = c.post("/chat", json={"query": "second", "session_id": sid})
        assert r2.json()["session_id"] == sid


class TestChatStream:
    def _parse_sse(self, raw: str) -> list[tuple[str, str]]:
        """非常宽松的 SSE 解析：返回 (event, data) 列表。"""
        out: list[tuple[str, str]] = []
        cur_event = "message"
        cur_data: list[str] = []
        for line in raw.splitlines():
            if line.startswith("event: "):
                cur_event = line[len("event: "):]
            elif line.startswith("data: "):
                cur_data.append(line[len("data: "):])
            elif line == "":
                if cur_data:
                    out.append((cur_event, "\n".join(cur_data)))
                cur_event = "message"
                cur_data = []
        if cur_data:
            out.append((cur_event, "\n".join(cur_data)))
        return out

    def test_stream_event_sequence(self, client):
        c, m = client
        m._agent = _FakeAgent()
        with c.stream("POST", "/chat/stream", json={"query": "hello"}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            body = "".join(chunk for chunk in r.iter_text())
        events = self._parse_sse(body)
        types = [e for e, _ in events]
        assert types[0] == "session"
        assert "meta" in types
        assert "tool_result" in types
        assert "token" in types
        assert types[-1] == "done"

    def test_stream_pipeline_exception_emits_error_event(self, client):
        c, m = client

        class _BoomAgent:
            def handle_turn_stream(self, *a, **kw):
                yield {"type": "meta", "data": {}}
                raise RuntimeError("pipeline boom")

        m._agent = _BoomAgent()
        with c.stream("POST", "/chat/stream", json={"query": "x"}) as r:
            body = "".join(chunk for chunk in r.iter_text())
        events = self._parse_sse(body)
        # 应该有 session + meta + error
        types = [e for e, _ in events]
        assert "error" in types

    def test_stream_session_id_in_first_event(self, client):
        c, m = client
        m._agent = _FakeAgent()
        with c.stream("POST", "/chat/stream", json={"query": "x"}) as r:
            body = "".join(chunk for chunk in r.iter_text())
        events = self._parse_sse(body)
        assert events[0][0] == "session"
        import json as _json
        sid = _json.loads(events[0][1])["session_id"]
        assert sid
