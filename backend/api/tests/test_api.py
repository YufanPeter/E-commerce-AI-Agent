"""FastAPI 端点测试：覆盖 /chat 与 /chat/stream 的 happy + edge case。

用 TestClient + mock Agent，避免真实 LLM 调用。
"""

from __future__ import annotations

from time import perf_counter
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

    def handle_image_turn_stream(self, image, session, hint_text="") -> Iterator[dict]:
        # 记录收到的图片，供断言路由是否走了图搜
        self.received_image = image
        self.received_hint = hint_text
        yield {"type": "meta", "data": {"decision": {"tool": "recommend", "rewritten_query": "视觉query", "confidence": "high", "reasoning": "image"}, "trace": {"timings": {}, "extracted_query": "视觉query"}}}
        yield {"type": "tool_result", "data": {"tool_name": "recommend", "payload": {"products": [{"product_id": "p_clothes_001"}], "summary": {"source": "visual_search"}}}}
        yield {"type": "token", "data": "根据图片找到相似商品"}
        yield {"type": "done", "data": {"timings": {}, "narrative": "根据图片找到相似商品"}}


class TestHealthz:
    def test_ok(self, client):
        c, _ = client
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_warmup_status_shape(self, client):
        c, _ = client
        r = c.get("/warmup")
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert "message" in body


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

    def test_stream_startup_events_do_not_wait_for_agent(self, client):
        _, m = client
        from agent.session import AgentSession

        req = m.ChatRequest(query="hello")
        session = AgentSession()
        with patch.object(
            m,
            "_get_agent",
            side_effect=AssertionError("agent loaded too early"),
        ) as get_agent:
            gen = m._chat_event_generator(req, session, request_started_at=perf_counter())
            first = next(gen)
            second = next(gen)
            gen.close()

        assert "event: session" in first
        assert "event: status" in second
        assert '"phase": "startup"' in second
        get_agent.assert_not_called()


class TestCartReset:
    def test_reset_clears_cart(self, client):
        c, m = client
        with patch.object(m, "CartStore") as MockStore:
            MockStore.return_value.clear.return_value = 3
            r = c.post("/cart/reset")
        assert r.status_code == 200
        assert r.json() == {"status": "cleared", "removed": 3}
        MockStore.return_value.clear.assert_called_once_with()


class TestCompareEndpoint:
    def test_too_few_ids_rejected(self, client):
        c, _ = client
        r = c.post("/compare", json={"product_ids": ["p1"]})
        assert r.status_code == 400

    def test_happy_path_returns_comparison(self, client):
        c, _ = client

        fake = {
            "title": "对比：A vs B",
            "products": [{"product_id": "p1", "title": "A"}, {"product_id": "p2", "title": "B"}],
            "rows": [{"label": "价格", "values": ["¥1", "¥2"], "highlight": 0}],
            "recommendation": "选 A。",
        }

        class _D:  # 占位 detail，只要非 None 即可通过有效性校验
            pass

        with patch("store.product_store.ProductStore.get_product_detail", return_value=_D()), \
             patch("agent.comparison.build_comparison", return_value=fake):
            r = c.post("/compare", json={"product_ids": ["p1", "p2"], "focus": "续航"})
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "对比：A vs B"
        assert len(body["rows"]) == 1

    def test_fewer_than_two_valid_products(self, client):
        c, _ = client
        with patch("store.product_store.ProductStore.get_product_detail", return_value=None):
            r = c.post("/compare", json={"product_ids": ["x1", "x2"]})
        assert r.status_code == 404


class TestResolveImage:
    def _req(self, **kw):
        from api.main import ChatRequest
        return ChatRequest(query=kw.pop("query", ""), **kw)

    def test_no_image_returns_none(self):
        from api.main import _resolve_image
        assert _resolve_image(self._req()) is None

    def test_image_url_takes_priority(self):
        from api.main import _resolve_image
        req = self._req(image_url="https://x/y.jpg", image_base64="abc")
        assert _resolve_image(req) == "https://x/y.jpg"

    def test_raw_base64_gets_data_uri_prefix(self):
        from api.main import _resolve_image
        req = self._req(image_base64="/9j/abcd")
        assert _resolve_image(req) == "data:image/jpeg;base64,/9j/abcd"

    def test_data_uri_passthrough(self):
        from api.main import _resolve_image
        data_uri = "data:image/png;base64,iVBOR"
        req = self._req(image_base64=data_uri)
        assert _resolve_image(req) == data_uri


class TestVisualSearchStream:
    def _parse_sse(self, raw: str):
        out = []
        cur_event, cur_data = "message", []
        for line in raw.splitlines():
            if line.startswith("event: "):
                cur_event = line[len("event: "):]
            elif line.startswith("data: "):
                cur_data.append(line[len("data: "):])
            elif line == "":
                if cur_data:
                    out.append((cur_event, "\n".join(cur_data)))
                cur_event, cur_data = "message", []
        if cur_data:
            out.append((cur_event, "\n".join(cur_data)))
        return out

    def test_image_request_routes_to_visual_search(self, client):
        c, m = client
        agent = _FakeAgent()
        m._agent = agent
        with c.stream(
            "POST", "/chat/stream",
            json={"query": "", "image_base64": "/9j/zzz"},
        ) as r:
            assert r.status_code == 200
            body = "".join(chunk for chunk in r.iter_text())
        # 走了图搜入口，且 base64 被补上 data URI 前缀
        assert agent.received_image == "data:image/jpeg;base64,/9j/zzz"
        events = self._parse_sse(body)
        types = [e for e, _ in events]
        assert types[0] == "session"
        assert "tool_result" in types
        assert types[-1] == "done"

    def test_text_request_does_not_route_to_visual(self, client):
        c, m = client
        agent = _FakeAgent()
        m._agent = agent
        with c.stream("POST", "/chat/stream", json={"query": "推荐耳机"}) as r:
            body = "".join(chunk for chunk in r.iter_text())
        # 纯文本不应触发图搜
        assert not hasattr(agent, "received_image")


class TestGetCart:
    def test_returns_snapshot_shape(self, client):
        c, m = client

        class _Line:
            quantity = 2

            @property
            def subtotal(self):
                return 100.0

            def to_dict(self):
                return {"product_id": "p1", "quantity": 2, "subtotal": 100.0}

        with patch("api.main.CartStore") as MockStore:
            MockStore.return_value.list_items.return_value = [_Line()]
            r = c.get("/cart")
        assert r.status_code == 200
        body = r.json()
        assert body["cart"]["item_count"] == 2
        assert body["cart"]["total"] == 100.0
        assert body["cart"]["lines"][0]["product_id"] == "p1"


class TestCartMutate:
    def test_remove_deletes_line_and_returns_snapshot(self, client):
        c, _ = client

        with patch("api.main.CartStore") as MockStore:
            store = MockStore.return_value
            store.remove_item.return_value = True
            store.list_items.return_value = []
            r = c.post("/cart/mutate", json={"action": "remove", "cartItemID": "12"})

        assert r.status_code == 200
        assert r.json()["cart"]["lines"] == []
        store.remove_item.assert_called_once_with(12)

    def test_remove_missing_line_returns_404(self, client):
        c, _ = client

        with patch("api.main.CartStore") as MockStore:
            MockStore.return_value.remove_item.return_value = False
            r = c.post("/cart/mutate", json={"action": "remove", "cartItemID": "12"})

        assert r.status_code == 404


class TestTitleEndpoint:
    def test_empty_user_text_rejected(self, client):
        c, _ = client
        r = c.post("/title", json={"user_text": "  "})
        assert r.status_code == 400

    def test_llm_failure_falls_back_to_truncation(self, client):
        c, _ = client
        # 让 get_client 抛错 → 退回截句标题
        with patch("llm.client.get_client", side_effect=RuntimeError("boom")):
            r = c.post("/title", json={"user_text": "推荐一款适合油皮的洗面奶啊啊啊啊啊啊啊"})
        assert r.status_code == 200
        title = r.json()["title"]
        assert title  # 非空
        assert len(title) <= 12

    def test_happy_path_uses_llm_title(self, client):
        c, _ = client

        class _Msg:
            content = "油皮洗面奶"

        class _Resp:
            choices = [type("C", (), {"message": _Msg()})()]

        class _FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": self})()

            def create(self, **kw):
                return _Resp()

        with patch("llm.client.get_client", return_value=_FakeClient()), \
             patch("llm.client.get_model_id", return_value="m"):
            r = c.post("/title", json={"user_text": "推荐油皮洗面奶", "assistant_text": "给你推荐几款"})
        assert r.status_code == 200
        assert r.json()["title"] == "油皮洗面奶"
