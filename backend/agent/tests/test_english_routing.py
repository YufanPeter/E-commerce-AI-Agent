"""English turns: the router's arguments are checked, then turned into tool slots."""

from __future__ import annotations

from agent.amazon import unrouted
from agent.intent_router import IntentDecision, _english_state, english_args
from agent.session import AgentSession
from agent.turn_args import routed_slots

ITEMS = [{"product_id": f"p{i}", "title": f"Speaker {i}"} for i in range(1, 4)]


def _session() -> AgentSession:
    session = AgentSession()
    session.remember_search({"sub_category": "Bluetooth Speakers"}, list(ITEMS))
    return session


def _slots(tool: str, session: AgentSession, **args) -> dict:
    return routed_slots(IntentDecision(tool, "", "high", "", english_args(args)), session)


def test_malformed_arguments_read_as_not_said():
    args = english_args({"positions": [2, 2, 0, "3", True, 150], "quantity": -1, "cart_action": "buy",
                         "size_step": 5, "clear": ["brand", "color"], "cheaper": "yes"})
    assert args["positions"] == [2]
    assert (args["quantity"], args["cart_action"], args["size_step"], args["clear"], args["cheaper"]) == (None, None, 0, ["brand"], False)


def test_positions_are_checked_against_the_screen():
    session = _session()
    assert _slots("product_detail", session, positions=[3, 7])["target"]["indices"] == [2]
    assert _slots("compare", session, positions=[3, 1])["product_ids"] == ["p3", "p1"]
    assert "product_ids" not in _slots("compare", session, positions=[1])


def test_an_unplaced_detail_question_is_about_the_focused_product():
    session = _session()
    session.task.focus_on("p2")
    assert _slots("product_detail", session)["target"]["indices"] == [1]
    assert _slots("cart", session, cart_action="add")["target"]["indices"] == []  # The cart asks instead.


def test_cart_and_search_arguments_reach_their_slots():
    session = _session()
    cart = _slots("cart", session, cart_action="change_spec", cart_item=2, quantity=3, size_step=-1)
    assert (cart["cart_action"], cart["cart_item"], cart["quantity"], cart["spec_update"]) == ("change_spec", 2, 3, {"step": -1})
    assert _slots("refine", session, clear=["budget"], cheaper=True) == {"constraint_ops": {"clear": ["budget"]}, "cheaper": True}


def test_the_router_sees_what_the_shopper_can_point_at():
    session = _session()
    session.task.focus_on("p2")
    session.task.ask("cart_sku", {"title": "Speaker 2"})
    state = "\n".join(_english_state(session))
    assert "1. Speaker 1" in state and "Focused product: 2. Speaker 2" in state
    assert "choose a size, color or other option of Speaker 2" in state


def test_without_the_router_only_a_first_message_is_searched():
    assert unrouted("Bluetooth speakers", AgentSession()).tool == "recommend"
    follow_up = unrouted("the second one", _session())
    assert follow_up.tool == "fallback" and follow_up.args == {"unavailable": True}
