"""/chat/stream 断连：用真实 uvicorn 和真实 socket 断开，验证收尾确实发生。

TestClient 不会模拟客户端中途断开，这条链路只有真断连才能测到。
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import uvicorn

from agent.orchestrator import Agent
from agent.tools.base import ToolResult
from store.session_store import SqliteSessionStore


class _EndlessStream:
    """模型一直在生成，直到被关闭。没有被关闭的话测试会超时失败。"""

    def __init__(self):
        self.closed = threading.Event()
        self.chunks_after_close = 0

    def __iter__(self):
        yield _chunk("开场｜为你找到几款\n")
        yield _chunk("商品｜p1｜轻\n")
        # composer 按整行向外吐事件，生成器只有在吐事件时才能响应关闭，所以每块都是完整一行。
        for index in range(2, 600):
            if self.closed.is_set():
                self.chunks_after_close += 1
            time.sleep(0.02)
            yield _chunk(f"商品｜p{index}｜稳\n")

    def close(self):
        self.closed.set()


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class _RecommendStub:
    name = "recommend"

    def run(self, query, session, slots):
        session.remember_search({"query": query}, [{"product_id": "p1", "title": "A"}])
        return ToolResult(tool_name="recommend", payload={"query": query, "products": []}, composer_hint="h")


class _Static:
    def __init__(self, name):
        self.name = name

    def run(self, query, session, slots):
        return ToolResult(tool_name=self.name, payload={}, narrative_override="x", needs_composer=False)


def _router_resp():
    args = json.dumps({"tool": "recommend", "rewritten_query": "耳机", "confidence": "high", "reasoning": ""})
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=args))], content=None))])


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    from api import main as api_main

    tools = {n: _Static(n) for n in ("refine", "compare", "product_detail", "cart", "clarify", "fallback")}
    tools["recommend"] = _RecommendStub()
    store = SqliteSessionStore(db_path=tmp_path / "sessions.sqlite3")
    monkeypatch.setattr(api_main, "_sessions", store)
    monkeypatch.setattr(api_main, "_get_agent", lambda: Agent(tools=tools))
    monkeypatch.setattr(api_main, "_attach_user_memory", lambda *a, **k: None)
    monkeypatch.setattr(api_main, "_turn_gate", api_main._SessionTurnGate())

    server = uvicorn.Server(uvicorn.Config(
        api_main.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", store, api_main
    server.should_exit = True
    thread.join(timeout=5)


def test_client_disconnect_stops_llm_and_saves_partial_turn(live_server):
    base_url, store, api_main = live_server
    upstream = _EndlessStream()
    session_id = "disconnect-test"

    with patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = upstream

        with httpx.Client(timeout=10) as client:
            with client.stream(
                "POST",
                f"{base_url}/chat/stream",
                json={"query": "耳机", "session_id": session_id},
            ) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if '"part": "item"' in line:
                        break
            # 离开 with：连接被关闭，服务端应收到断连

        assert upstream.closed.wait(timeout=5), "LLM stream was not closed after disconnect"

        deadline = time.monotonic() + 5
        while session_id in api_main._turn_gate._active:
            assert time.monotonic() < deadline, "turn gate was not released"
            time.sleep(0.01)

    session = store.get_or_create(session_id)
    assert [m.role for m in session.history] == ["user", "assistant"]
    saved = json.loads(session.history[-1].content)
    assert saved["interrupted"] is True
    assert saved["items"][0]["productId"] == "p1"
    assert upstream.chunks_after_close == 0


def test_turn_gate_makes_next_turn_wait_for_previous_one():
    from api.main import _SessionTurnGate

    gate = _SessionTurnGate(wait_seconds=5)
    first = gate.begin("s")
    entered = threading.Event()

    def next_turn():
        gate.begin("s")
        entered.set()

    threading.Thread(target=next_turn, daemon=True).start()
    assert not entered.wait(timeout=0.1)
    gate.finish("s", first)
    assert entered.wait(timeout=2)


def test_turn_gate_gives_up_waiting_on_a_stuck_turn():
    from api.main import _SessionTurnGate

    gate = _SessionTurnGate(wait_seconds=0.05)
    stuck = gate.begin("s")
    second = gate.begin("s")
    assert second is not stuck
    # 旧回合晚到的 finish 不能把新回合的登记删掉
    gate.finish("s", stuck)
    assert gate._active["s"] is second


def test_abandoned_turn_hands_its_question_to_the_next_one_and_cannot_save():
    from api.main import _SessionTurnGate

    gate = _SessionTurnGate(wait_seconds=0.01)
    stuck = gate.begin("s", "推荐跑鞋")
    second = gate.begin("s", "只要便宜的")
    assert second.replay == ["推荐跑鞋"]
    assert stuck.abandoned
    assert gate.claim_save(stuck) is False
    assert gate.claim_save(second) is True


def test_consecutive_abandoned_turns_keep_every_question():
    from api.main import _SessionTurnGate

    gate = _SessionTurnGate(wait_seconds=0.01)
    gate.begin("s", "推荐跑鞋")
    gate.begin("s", "只要便宜的")
    assert gate.begin("s", "要白色的").replay == ["推荐跑鞋", "只要便宜的"]


def test_turn_being_saved_is_not_abandoned():
    from api.main import _SessionTurnGate

    gate = _SessionTurnGate(wait_seconds=0.01, save_grace_seconds=0.01)
    saving = gate.begin("s", "推荐跑鞋")
    assert gate.claim_save(saving) is True
    assert gate.begin("s", "只要便宜的").replay == []
    assert not saving.abandoned


def test_stuck_turn_does_not_overwrite_the_amendment(live_server):
    """旧回合卡在工具里时用户改了需求：新回合立刻开始并接上原问题，旧回合晚到也不能写会话。"""
    base_url, store, api_main = live_server
    api_main._turn_gate = api_main._SessionTurnGate(wait_seconds=0.1)
    session_id = "amend-test"
    release = threading.Event()
    agent = api_main._get_agent()
    original_run = agent._tools["recommend"].run

    calls = []

    def run(query, session, slots):
        calls.append(query)
        if len(calls) == 1:  # 只卡第一个回合
            release.wait(timeout=5)
        return original_run(query, session, slots)

    agent._tools["recommend"].run = run
    monkey_agent = lambda: agent  # noqa: E731
    with patch.object(api_main, "_get_agent", monkey_agent), \
            patch("agent.intent_router.get_client") as rc, patch("agent.composer.get_client") as cc:
        rc.return_value.chat.completions.create.return_value = _router_resp()
        cc.return_value.chat.completions.create.return_value = [_chunk("开场｜新结果\n")]

        with httpx.Client(timeout=10) as client:
            with client.stream(
                "POST", f"{base_url}/chat/stream", json={"query": "推荐跑鞋", "session_id": session_id}
            ) as response:
                for line in response.iter_lines():
                    if '"phase": "tool"' in line or "正在搜索" in line:
                        break
            stuck = api_main._turn_gate._active[session_id]
            with client.stream(
                "POST", f"{base_url}/chat/stream", json={"query": "只要便宜的", "session_id": session_id}
            ) as response:
                for _ in response.iter_lines():
                    pass

        # 新回合已经答完，这时旧回合才从工具里出来
        assert stuck.abandoned
        release.set()
        assert stuck.finished.wait(timeout=5), "abandoned turn never finished"

    history = store.get_or_create(session_id).history
    assert [m.content for m in history if m.role == "user"] == ["推荐跑鞋", "只要便宜的"]
    assert [m.role for m in history] == ["user", "assistant", "user", "assistant"]
    assert "中断" in history[1].content


def test_turn_gate_ignores_requests_without_session_id():
    from api.main import _SessionTurnGate

    gate = _SessionTurnGate()
    assert gate.begin(None) is None
    gate.finish(None, None)
