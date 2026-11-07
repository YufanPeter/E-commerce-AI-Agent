from __future__ import annotations

from agent.intent_router import IntentDecision
from agent.orchestrator import Agent
from agent.session import AgentSession


def _decision(tool: str) -> IntentDecision:
    return IntentDecision(tool=tool, rewritten_query="下一个推荐", confidence="high", reasoning="")


def test_relative_reference_with_focus_goes_to_product_detail():
    session = AgentSession()
    session.remember_search({}, [{"product_id": "p1", "title": "A"}, {"product_id": "p2", "title": "B"}])
    session.set("last_focus_product_id", "p1")
    out = Agent._apply_post_route_guards("下一个呢", session, _decision("refine"))
    assert out.tool == "product_detail"
    assert out.rewritten_query == "下一个呢"


def test_relative_reference_without_focus_keeps_router_decision():
    session = AgentSession()
    session.remember_search({}, [{"product_id": "p1", "title": "A"}])
    out = Agent._apply_post_route_guards("下一个呢", session, _decision("refine"))
    assert out.tool == "refine"


def test_absolute_last_is_not_relative():
    session = AgentSession()
    session.remember_search({}, [{"product_id": "p1", "title": "A"}, {"product_id": "p2", "title": "B"}])
    session.set("last_focus_product_id", "p1")
    out = Agent._apply_post_route_guards("最后一个详细说说", session, _decision("recommend"))
    assert out.tool == "recommend"  # 交给原有逻辑（product_detail guard 等）处理
