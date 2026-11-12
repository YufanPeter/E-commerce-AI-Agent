"""English memory: plain statements, acknowledging them, and brand habits from activity."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import memory_inference, memory_rules_en, memory_service
from agent.intent_router import IntentDecision
from agent.memory_rules_en import Statement, read_statements
from agent.orchestrator import _acknowledge_memory
from agent.session import AgentSession
from agent.tools.base import ToolResult
from store.user_memory_store import UserMemoryStore

BRANDS = {"maybelline": "MAYBELLINE", "sony": "Sony", "jbl": "JBL", "apple": "Apple"}


def _brand(name: str) -> str | None:
    return BRANDS.get(name.split()[0])


@pytest.mark.parametrize("text, expected", [
    ("I have sensitive skin and I never buy Maybelline",
     [Statement("skin_type", "sensitive"), Statement("brand_avoid", "MAYBELLINE")]),
    ("Remember that I avoid fragrance", [Statement("avoid_term", "fragrance")]),  # an ingredient, not a brand
    ("my skin is very oily, and I hate sulfates", [Statement("skin_type", "oily"), Statement("avoid_term", "sulfate")]),
    ("I usually wear size 10 shoes", [Statement("shoe_size", "US 10")]),
    ("I'm allergic to parabens", [Statement("allergy", "paraben")]),
    ("I stick with JBL speakers", [Statement("brand_like", "JBL")]),
    ("I never buy anything from Apple", [Statement("brand_avoid", "Apple")]),
])
def test_plain_statements_are_read_clause_by_clause(text, expected):
    assert read_statements(text, _brand) == expected


@pytest.mark.parametrize("text", ["I avoid cheap stuff", "I don't like it", "I need a speaker for my mom"])
def test_words_that_are_neither_a_brand_nor_an_ingredient_are_left_alone(text):
    assert read_statements(text, _brand) == []


@pytest.fixture
def english(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_service, "is_english", lambda: True)
    monkeypatch.setattr(memory_rules_en, "catalog_brand", _brand)
    return UserMemoryStore(db_path=tmp_path / "memory.sqlite3")


def test_a_statement_takes_effect_and_leaves_words_for_the_reply(english):
    memory = memory_service.remember_from_turn(english, "u1", "I avoid fragrance and I never buy Maybelline", "s1")
    preference = english.get_preference("u1")
    assert preference.avoid_terms == ["fragrance"] and preference.brand_exclude == ["MAYBELLINE"]
    assert memory.notes == ["you avoid fragrance", "you don't buy MAYBELLINE"]


def test_an_allergy_waits_for_confirmation(english):
    memory = memory_service.remember_from_turn(english, "u1", "I'm allergic to parabens", "s1")
    assert memory.events == [] and english.get_preference("u1").avoid_terms == []
    assert [row["value"] for row in english.list_items("u1", statuses=("suggested",))] == ["paraben"]
    assert "confirm it on your Memory page" in memory.notes[0]


def test_a_gift_for_someone_else_is_not_about_the_shopper(english):
    assert memory_service.remember_from_turn(english, "u1", "I never buy Maybelline for my mom", "s1").notes == []


def _with_notes(*notes: str) -> AgentSession:
    session = AgentSession()
    session.turn_memory = list(notes)
    return session


def test_a_message_that_only_tells_about_the_shopper_is_answered_with_what_was_kept():
    result = ToolResult(tool_name="clarify", payload={}, narrative_override="What are you shopping for?", needs_composer=False)
    acknowledged = _acknowledge_memory(result, IntentDecision("clarify", "", "high", ""), _with_notes("you avoid fragrance"))
    assert "you avoid fragrance" in acknowledged.narrative_override


def test_a_search_mentions_the_fact_in_passing():
    result = ToolResult(tool_name="recommend", payload={}, composer_hint="Found 5 products.")
    acknowledged = _acknowledge_memory(result, IntentDecision("recommend", "", "high", ""), _with_notes("you avoid fragrance"))
    assert acknowledged.composer_hint.startswith("Found 5 products.") and "you avoid fragrance" in acknowledged.composer_hint
    assert _acknowledge_memory(result, IntentDecision("recommend", "", "high", ""), AgentSession()) is result


def test_brand_habits_group_spellings_and_skip_generic_brands(monkeypatch):
    monkeypatch.setattr(memory_inference, "is_english", lambda: True)
    monkeypatch.setattr(memory_rules_en, "catalog_brand", lambda name: None if name == "Generic" else name)
    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    events = [
        {"type": "add_cart", "brand": "Maybelline New York", "created_at": "2026-11-11 10:00:00"},
        {"type": "view_detail", "brand": "MAYBELLINE", "created_at": "2026-11-11 10:00:00"},
        {"type": "view_detail", "brand": "Maybelline New York", "created_at": "2026-11-11 10:00:00"},
        {"type": "checkout", "brand": "Generic", "created_at": "2026-11-11 10:00:00"},
        {"type": "checkout", "brand": "Generic", "created_at": "2026-11-11 10:00:00"},
    ]
    suggestions = memory_inference.infer_suggestions(events, now)
    assert [s.key.value for s in suggestions] == ["Maybelline New York"]
    assert suggestions[0].evidence == "Recently added to cart 1 time, viewed 2 times"
