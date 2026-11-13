"""Conversation digests: what a chat was about, recalled or picked up in a later one."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import conversation_digest as digest
from agent.intent_router import IntentDecision, _english_state, english_args
from agent.memory_service import events_from_tool_result
from agent.session import AgentSession
from agent.tools.knowledge import KnowledgeTool
from agent.turn_args import routed_slots
from store.user_memory_store import UserMemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_EXTRACT", "0")  # The model's rewrite runs in the background; not here.
    return UserMemoryStore(db_path=tmp_path / "memory.sqlite3")


def _gift_chat(session_id: str = "s1") -> AgentSession:
    session = AgentSession(session_id=session_id, user_id="u1")
    session.add_user("noise cancelling earbuds for my mom under $100")
    session.add_assistant("Here are five earbuds.")
    session.remember_search(
        {"sub_category": "Earbud Headphones", "max_price": 100.0},
        [{"product_id": "sony", "title": "Sony WF-1000XM4, Wireless Earbuds"}, {"product_id": "jbl", "title": "JBL Tune 230NC"}],
    )
    return session


def _events(store: UserMemoryStore, session_id: str = "s1") -> None:
    store.record_events("u1", session_id, [
        {"type": "search", "sub_category": "Earbud Headphones", "payload": {"query": "earbuds for my mom"}},
        {"type": "compare", "product_id": "sony", "title": "Sony WF-1000XM4, Wireless Earbuds"},
        {"type": "compare", "product_id": "jbl", "title": "JBL Tune 230NC"},
        {"type": "add_cart", "product_id": "jbl", "title": "JBL Tune 230NC"},
    ])


def test_facts_come_from_the_conversation_itself(store):
    _events(store)
    facts = digest.digest_facts(_gift_chat(), store.session_events("u1", "s1"))
    assert facts["types"] == ["Earbud Headphones"] and facts["max_price"] == 100.0
    assert facts["compared"] == ["Sony WF-1000XM4, Wireless Earbuds", "JBL Tune 230NC"] and facts["added"] == ["JBL Tune 230NC"]
    assert [p["product_id"] for p in facts["products"]] == ["sony", "jbl"]
    assert digest.facts_summary(facts) == (
        "Looked for Earbud Headphones under $100; compared Sony WF-1000XM4, JBL Tune 230NC; added JBL Tune 230NC"
    )


def test_each_turn_refreshes_the_facts_and_keeps_the_written_summary(store):
    _events(store)
    session = _gift_chat()
    digest.update_digest(store, session)
    store.save_digest_text("u1", "s1", "Gift earbuds for mom", "You looked for earbuds for your mom.", digest._turns(session))
    digest.update_digest(store, session)  # Same turn count: the facts refresh, the model's words stay.
    [row] = store.list_digests("u1")
    assert (row["title"], row["summary"]) == ("Gift earbuds for mom", "You looked for earbuds for your mom.")
    session.add_user("thanks")
    digest.update_digest(store, session)
    store.save_digest_text("u1", "s1", "Stale", "Written for an older turn.", digest._turns(session) - 2)
    assert store.list_digests("u1")[0]["title"] == "Gift earbuds for mom"  # A late rewrite never wins.


def test_small_talk_and_switched_off_memory_leave_no_digest(store):
    chat = AgentSession(session_id="s2", user_id="u1")
    chat.add_user("hi")
    digest.update_digest(store, chat)
    off = _gift_chat("s3")
    off.user_profile = {"personalization_enabled": False}
    digest.update_digest(store, off)
    assert store.list_digests("u1") == []


def test_the_router_sees_earlier_conversations_but_not_this_one(store):
    _events(store)
    digest.update_digest(store, _gift_chat())
    assert digest.conversation_trail(store, "u1", exclude_session="s1") == []
    trail = digest.conversation_trail(store, "u1", exclude_session="now")
    session = AgentSession()
    session.conversation_trail = trail
    state = "\n".join(_english_state(session))
    assert "history_conversation counts these" in state and "Looked for Earbud Headphones under $100" in state
    decision = IntentDecision("resume", "", "high", "", english_args({"history_conversation": 1}))
    assert routed_slots(decision, session)["history_conversation"]["session_id"] == "s1"


def _resume_tool(products: dict[str, float]):
    def hits_for(ids, max_price=None, popular_first=False):
        return [SimpleNamespace(product_id=i, title=i, brand="B", category="C", sub_category="L", min_price=p, max_price=p,
                                sku_count=1, evidence=[], attribute_checks={}, graph_evidence=())
                for i, p in ((i, products[i]) for i in ids if i in products)]

    service = SimpleNamespace(graph=None, hits_for=hits_for)
    recommend = SimpleNamespace(_get_service=lambda: service, _attach_price_displays=lambda cards, parsed: None)
    return KnowledgeTool("resume", recommend)


def test_resuming_puts_that_conversation_back_on_screen(monkeypatch):
    monkeypatch.setattr("agent.tools.knowledge._card_with_evidence", lambda hit, query: {"product_id": hit.product_id})
    convo = {
        "session_id": "s1", "title": "Gift earbuds for mom", "at": "2026-11-12", "summary": "You added the JBL.",
        "facts": {"products": [{"product_id": "sony"}, {"product_id": "jbl"}],
                  "constraints": {"sub_category": "Earbud Headphones", "max_price": 100.0}},
    }
    session = AgentSession()
    result = _resume_tool({"sony": 90.0, "jbl": 50.0}).run("let's continue the gift search", session, {"history_conversation": convo})
    assert [p["product_id"] for p in result.payload["products"]] == ["sony", "jbl"]
    assert "Gift earbuds for mom" in result.composer_hint and "2026-11-12" in result.composer_hint
    # Follow-ups carry on from that conversation's search, and "the second one" is the JBL.
    assert session.recall_parsed()["max_price"] == 100.0 and session.recall_hits()[1]["product_id"] == "jbl"
    # Bringing a conversation back is not a search the shopper typed.
    assert events_from_tool_result(result.to_dict(), "let's continue the gift search") == []


def test_resuming_without_products_recaps_in_words():
    convo = {"session_id": "s1", "title": "Gift ideas", "at": "2026-11-12", "summary": "You asked about gifts.", "facts": {}}
    result = _resume_tool({}).run("what did we talk about?", AgentSession(), {"history_conversation": convo})
    assert result.narrative_override.startswith("On 2026-11-12 we talked about Gift ideas: You asked about gifts.")
    session = AgentSession()
    assert "don't have an earlier conversation" in _resume_tool({}).run("what did we talk about?", session, {}).narrative_override


def test_a_chat_that_only_recalled_another_is_not_remembered_itself(store):
    recall = AgentSession(session_id="s4", user_id="u1")
    recall.add_user("what did we talk about earlier?")
    recall.remember_search({"sub_category": "Earbud Headphones"}, [{"product_id": "jbl", "title": "JBL Tune 230NC"}])
    digest.update_digest(store, recall)  # Products on screen, but nothing done in this chat.
    assert store.list_digests("u1") == []
