"""Products from earlier visits: "something like the speaker I bought last time"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import memory_service
from agent.intent_router import IntentDecision, _english_state, english_args
from agent.session import AgentSession
from agent.tools.knowledge import KnowledgeTool
from agent.tools.recommend import RecommendTool
from agent.turn_args import routed_slots
from search.query_understanding import ParsedQuery
from search.search_service import SearchResult
from store.user_memory_store import UserMemoryStore

SPEAKER = {"product_id": "spk", "title": "BUGANI Portable Speaker", "sub_category": "Bluetooth Speakers"}


@pytest.fixture
def store(tmp_path):
    return UserMemoryStore(db_path=tmp_path / "memory.sqlite3")


def _event(kind, product_id, title, sub_category="Bluetooth Speakers"):
    return {"type": kind, "product_id": product_id, "title": title, "brand": "B", "sub_category": sub_category}


def test_the_trail_keeps_one_row_per_product_with_its_strongest_action(store):
    store.record_events("u1", "s1", [_event("view_detail", "spk", "Speaker"), _event("checkout", "spk", "Speaker")])
    store.record_events("u1", "s2", [_event("view_detail", "spk", "Speaker"), _event("view_detail", "std", "Stand", "Laptop Stands")])
    store.record_events("u1", "s2", [{"type": "search", "payload": {"query": "speakers"}}])
    trail = memory_service.product_trail(store, "u1")
    assert [(row["product_id"], row["action"]) for row in trail] == [("std", "viewed"), ("spk", "bought")]
    assert memory_service.product_trail(store, None) == []


def test_picking_a_product_whose_option_is_still_open_is_remembered():
    events = memory_service.events_from_tool_result(
        {"tool_name": "cart", "payload": {"action": "ask_spec", "product_id": "hp", "title": "PowerLocus Headphones"}}, "add it",
    )
    assert [(e["type"], e["product_id"], e["title"]) for e in events] == [("choose", "hp", "PowerLocus Headphones")]


def _session_with_trail() -> AgentSession:
    session = AgentSession()
    session.remember_search({"sub_category": "Coffee Mugs"}, [{"product_id": "mug", "title": "Llama Mug"}])
    session.product_trail = [{**SPEAKER, "action": "bought", "at": "2026-11-10"}]
    return session


def test_the_router_sees_earlier_products_apart_from_the_screen():
    state = "\n".join(_english_state(_session_with_trail()))
    assert "1. Llama Mug" in state
    assert "history_item counts these" in state and "1. bought on 2026-11-10: BUGANI Portable Speaker" in state


def test_a_history_item_reaches_the_tool_as_itself():
    session = _session_with_trail()

    def slots(tool, **args):
        return routed_slots(IntentDecision(tool, "", "high", "", english_args(args)), session)

    assert slots("similar", history_item=1)["history_product"]["product_id"] == "spk"
    assert slots("product_detail", history_item=1)["product_id"] == "spk"
    assert "history_product" not in slots("similar", history_item=4)  # out of range reads as not said
    assert "history_product" not in slots("similar", history_item=1, positions=[1])  # the screen wins


class _Service:
    graph = None

    def __init__(self):
        self.parsed: list[ParsedQuery | None] = []

    def min_price(self, product_id):
        return 25.96

    def search(self, query, top_k_chunks=50, top_k_products=10, base=None, parsed=None, **_):
        self.parsed.append(parsed)
        hits = [_hit(pid) for pid in ("spk", "jbl", "anker")]
        return SearchResult(parsed or ParsedQuery(original_query=query), hits, 3, 3)


def _hit(pid):
    return SimpleNamespace(
        product_id=pid, title=f"{pid} speaker", brand=pid, category="Electronics", sub_category="Bluetooth Speakers",
        base_price=20.0, score=1.0, best_distance=None, rerank_score=None, evidence=[], min_price=20.0, max_price=20.0,
        sku_count=1, attribute_checks={}, graph_evidence=(), rating=None, to_dict=lambda: {"product_id": pid},
    )


def test_similar_searches_the_remembered_products_own_type_and_leaves_it_out():
    service = _Service()
    store = SimpleNamespace(
        db_path=":memory:", get_products_by_ids=lambda ids: [],
        get_product_detail=lambda pid: SimpleNamespace(sub_category="Bluetooth Speakers"),
    )
    recommend = RecommendTool(search_service=service, decomposer=lambda q: [], product_store=store)
    session = _session_with_trail()
    remembered = session.product_trail[0]
    result = KnowledgeTool("similar", recommend).run("something like the speaker I bought", session, {"history_product": remembered})
    assert service.parsed[-1].sub_category == "Bluetooth Speakers"  # not the mugs on screen
    assert [p["product_id"] for p in result.payload["products"]] == ["jbl", "anker"]
    assert "BUGANI Portable Speaker" in result.composer_hint and "bought on 2026-11-10" in result.composer_hint


def test_a_shopper_with_no_history_hears_so():
    recommend = RecommendTool(search_service=_Service(), decomposer=lambda q: [], product_store=SimpleNamespace())
    result = KnowledgeTool("similar", recommend).run("something like what I bought last time", AgentSession(), {})
    assert "don't have any products you bought" in result.narrative_override


def test_a_first_turn_is_cached_on_everything_the_router_reads(monkeypatch):
    from agent import intent_router

    calls = []
    monkeypatch.setattr(intent_router, "is_english", lambda: True)
    monkeypatch.setattr(intent_router, "_route_uncached", lambda q, s, t: calls.append(q) or IntentDecision("recommend", q, "high", ""))
    intent_router.clear_route_cache()

    def first_turn(trail):
        session = AgentSession()
        session.product_trail = trail
        return intent_router.route("bluetooth speaker", session)

    bought = [{**SPEAKER, "action": "bought", "at": "2026-11-10"}]
    first_turn(bought), first_turn(bought)
    assert len(calls) == 1  # A shopper with history still hits on the same opening.
    first_turn([{**SPEAKER, "action": "viewed", "at": "2026-11-11"}])
    assert len(calls) == 2  # Different history is a different question for the router.
    intent_router.clear_route_cache()
