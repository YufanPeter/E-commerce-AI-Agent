"""English copy: prompts, evidence, comparison facts, memory extraction and saved avoidances."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import catalog_config
import i18n
from agent import comparison, composer, memory_llm, memory_service
from agent.tools import evidence as evidence_module
from agent.tools import product_detail
from agent.tools.base import ToolResult
from agent.tools.evidence import card_evidence, relevant_excerpt
from search.catalog_search import CatalogSearchService
from search.query_understanding import ParsedQuery
from store.product_store import PriceRange, ProductDetail, ProductReview
from store.user_memory_store import ACTIVE, SUGGESTED, UserMemoryStore


@pytest.fixture()
def english(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = replace(catalog_config.PROFILE, locale="en", currency="USD")
    monkeypatch.setattr(catalog_config, "PROFILE", profile)
    monkeypatch.setattr(i18n, "PROFILE", profile)
    monkeypatch.setattr(i18n, "_ACTIVE", i18n.en.MESSAGES)
    for module in (comparison, memory_llm, memory_service, evidence_module, product_detail):
        monkeypatch.setattr(module, "is_english", lambda: True)


def _chunk(text: str, chunk_type: str, polarity: str = "") -> SimpleNamespace:
    return SimpleNamespace(document=text, metadata={"dataset": "amazon_reviews_2023", "chunk_type": chunk_type, "polarity": polarity})


def _detail(product_id: str, *, price: float = 20.0, category: str = "Electronics", attributes=None, reviews=()) -> ProductDetail:
    return ProductDetail(
        product_id=product_id, title=f"Watch {product_id}", brand="B", category=category, sub_category="Smartwatches",
        base_price=price, image_path=None, image_url=None,
        price_range=PriceRange(min_price=price, max_price=price, sku_count=1), skus=[],
        marketing_description="Waterproof to 50 meters. Comes in a gift box. Battery lasts a week.", faqs=[], reviews=list(reviews),
        catalog_source={"average_rating": 4.25, "rating_number": 1234}, attributes=attributes or {},
    )


# ------------------------------- Evidence -------------------------------


def test_excerpt_keeps_the_sentence_closest_to_the_request(english):
    text = "Comes in a gift box. Battery lasts a week on one charge. Available in three colors."
    assert relevant_excerpt(text, "long battery", 40) == "Battery lasts a week on one charge."


def test_reviews_are_labelled_as_reviews_not_listing_facts(english):
    hit = SimpleNamespace(evidence=[_chunk("Waterproof to 50 meters.", "marketing_description"), _chunk("Loved it on my swim.", "user_review", "positive")])
    assert [point["source"] for point in card_evidence(hit, "swim")["points"]] == ["Listing", "Customer review"]


def test_delivery_complaints_are_not_product_risks(english):
    late = SimpleNamespace(evidence=[_chunk("The package arrived late.", "user_review", "negative")])
    broken = SimpleNamespace(evidence=[_chunk("The strap broke in a week.", "user_review", "negative")])
    assert card_evidence(late, "strap") is None
    assert card_evidence(broken, "strap")["risk"] == "The strap broke in a week."


# ------------------------------- Composer -------------------------------


def _recommend_result() -> ToolResult:
    card = {
        "product_id": "p1", "title": "T", "rating": {"average": 4.2, "count": 900}, "attribute_checks": {"alcohol": "unknown"},
        "graph_evidence": [{"relation": "PRAISED_FOR", "value": "comfort", "quote": "so comfy"}],
    }
    return ToolResult("recommend", {"query": "q", "products": [card], "item_notes": {"p1": "Listing mentions “fits Series 5”."}}, composer_hint="Found 1 products.")


def test_english_prompt_gets_facts_hint_and_examples(english):
    messages = composer._build_messages(_recommend_result())
    assert messages[0]["content"] == i18n.en.COMPOSER_SYSTEM
    assert len(messages) == 2 + len(i18n.en.COMPOSER_FEW_SHOT)
    last = messages[-1]["content"]
    for fact in ("so comfy", '"unknown"', '"count": 900', "fits Series 5", "hint: Found 1 products."):
        assert fact in last


def test_detail_and_compare_use_their_own_prompts(english):
    detail = ToolResult("product_detail", {"query": "q", "product": {"title": "T"}, "rating": {"average": 4, "count": 2}})
    assert composer._build_messages(detail)[0]["content"] == i18n.en.DETAIL_SYSTEM
    assert '"rating"' in composer._build_messages(detail)[-1]["content"]
    compare = ToolResult("compare", {"comparison": {"title": "x", "products": [], "rows": []}})
    assert composer._build_messages(compare)[0]["content"] == i18n.en.COMPARE_SYSTEM


def test_line_protocol_parses_an_english_answer():
    parser = composer._ProtocolStreamParser()
    parser.feed("开场｜Here are two\n分组｜Lightest\n商品｜p1｜Weighs 20 grams\n总结｜Pick p1.\n追问｜Compare them\n")
    parser.finish()
    assert parser.to_structured() == {
        "opening": "Here are two", "items": [{"productId": "p1", "description": "Weighs 20 grams", "group": "Lightest"}],
        "followup": ["Compare them"], "summary": "Pick p1.",
    }


def test_tool_copy_is_returned_as_written():
    result = ToolResult("cart", {}, narrative_override="Added to cart.", needs_composer=False)
    assert composer.AnswerComposer().compose(result, SimpleNamespace()) == "Added to cart."


# ------------------------------- Detail -------------------------------


@pytest.mark.parametrize("query, focus", [
    ("What are the complaints about it?", "negative_reviews"),
    ("What do reviews say?", "reviews"),
    ("Will this irritate sensitive skin?", "sensitive_skin"),
    ("Does it run small?", "size_fit"),
    ("How is the battery?", "performance"),
    ("How do I clean it?", "usage"),
    ("Tell me about the second one", "general"),
])
def test_english_question_topics(english, query, focus):
    assert product_detail._detect_focus_aspect(query) == focus


def test_detail_evidence_reads_stored_text_closest_to_the_question(english):
    reviews = [
        ProductReview(0, "a", 5, "Pretty color and arrived fast.", "positive"),
        ProductReview(1, "b", 4, "The battery lasts days.", "positive"),
    ]
    found = product_detail._fallback_evidence(_detail("p", reviews=reviews), "reviews", 6, query="battery")
    assert [item["source_type"] for item in found][0] == "user_review"
    assert found[0]["text"] == "The battery lasts days."
    assert found[0]["title"] != "用户评价"


def test_detail_reports_real_rating_and_attributes(english, monkeypatch):
    monkeypatch.setattr(product_detail, "product_claims", lambda product_id: [{"relation": "PRAISED_FOR", "value": "comfort"}])
    facts = product_detail._catalog_facts(_detail("p", attributes={"material": ["leather"]}))
    assert facts["rating"] == {"average": 4.25, "count": 1234}
    assert facts["attributes"] == {"material": ["leather"]}
    assert facts["graph_claims"][0]["value"] == "comfort"


# ------------------------------- Comparison -------------------------------


def _rows(monkeypatch, details, claims=None) -> dict[str, list[str]]:
    monkeypatch.setattr(comparison, "product_claims", lambda product_id: (claims or {}).get(product_id, []))
    return {row["label"]: row["values"] for row in comparison.build_comparison(details)["rows"]}


def test_missing_attributes_show_unknown(english, monkeypatch):
    rows = _rows(monkeypatch, [_detail("a", attributes={"ingress_rating": ["IP68"], "battery_mah": ["260.0"]}), _detail("b", price=10)])
    assert rows["Water and dust rating"] == ["IP68", "Unknown"]
    assert rows["Battery capacity"] == ["260 mAh", "Unknown"]
    assert rows["Price"] == ["$20.00", "$10.00"]
    assert rows["Historical rating"] == ["4.25/5", "4.25/5"]


def test_rows_nobody_has_are_left_out(english, monkeypatch):
    assert "Storage" not in _rows(monkeypatch, [_detail("a"), _detail("b")])


def test_category_decides_which_attributes_appear(english, monkeypatch):
    attributes = {"spf": ["50"], "material": ["cotton"]}
    beauty = _rows(monkeypatch, [_detail(p, category="Beauty & Personal Care", attributes=attributes) for p in "ab"])
    clothing = _rows(monkeypatch, [_detail(p, category="Clothing & Sports", attributes=attributes) for p in "ab"])
    assert "SPF" in beauty and "Material" not in beauty
    assert "Material" in clothing and "SPF" not in clothing


def test_review_signals_are_their_own_rows(english, monkeypatch):
    claims = {"a": [{"relation": "PRAISED_FOR", "value": "battery_life"}, {"relation": "PRAISED_FOR", "value": "battery_life"}]}
    rows = _rows(monkeypatch, [_detail("a"), _detail("b")], claims)
    assert rows["Reviewers praise"] == ["Battery life", "Unknown"]
    assert "Reviewers criticize" not in rows


@pytest.mark.parametrize("value, shown", [("usb_c", "USB c"), ("gps", "GPS"), ("iphone_15", "iPhone 15"), ("IP67", "IP67"), ("2.5", "2.5")])
def test_stored_values_read_naturally(value, shown):
    assert comparison._readable(value) == shown


# ------------------------------- Memory -------------------------------


@pytest.mark.parametrize("text, expected", [
    ("I have oily skin", True),
    ("I'm allergic to fragrance", True),
    ("Find me a laptop today", False),
    ("Looking for a gift for my mom", False),
    ("smartwatches under $50", False),
])
def test_only_statements_about_the_shopper_reach_the_model(english, text, expected):
    assert memory_llm.might_disclose(text) is expected


def test_english_tool_description(english):
    tool = memory_llm._tool()
    assert tool["function"]["description"] == i18n.en.MEMORY_TOOL_DESCRIPTION
    assert memory_llm._TOOL["function"]["description"] != i18n.en.MEMORY_TOOL_DESCRIPTION


@pytest.fixture()
def memories(tmp_path) -> UserMemoryStore:
    return UserMemoryStore(tmp_path / "memory.sqlite3")


def test_tell_keeps_structure_and_asks_before_hiding_products(english, memories, monkeypatch):
    extracted = [memory_llm.ExtractedMemory("avoid_term", "fragrance", "allergic to fragrance", True)]
    monkeypatch.setattr(memory_service, "PROFILE", replace(catalog_config.PROFILE, capabilities=replace(catalog_config.PROFILE.capabilities, llm_memory=True)))
    monkeypatch.setattr(memory_service, "extract_with_llm", lambda text: extracted)
    monkeypatch.setattr(memory_service, "llm_extract_enabled", lambda: True)
    result = memory_service.tell(memories, "u", "I'm allergic to fragrance")
    assert result["updates"] == [] and "confirm" in result["message"]
    assert [row["value"] for row in memories.list_items("u", statuses=(SUGGESTED,))] == ["fragrance"]


def test_tell_keeps_unstructured_text_as_a_note(english, memories, monkeypatch):
    monkeypatch.setattr(memory_service, "extract_with_llm", lambda text: [])
    result = memory_service.tell(memories, "u", "Quiet products please")
    assert result["message"] == "Memory saved."
    assert memories.get_preference("u").preference_note == "Quiet products please"
    assert memories.list_items("u", statuses=(ACTIVE,))


def _service(contains=(), free_of=()) -> CatalogSearchService:
    service = CatalogSearchService.__new__(CatalogSearchService)
    service._attributes = {("p", "contains"): set(contains), ("p", "free_of"): set(free_of)}
    return service


def _intent(excluded=()) -> ParsedQuery:
    return ParsedQuery(original_query="", excluded_attributes=list(excluded))


def test_saved_avoidance_removes_products_that_state_it():
    assert _service(contains=["fragrance"])._attribute_checks("p", _intent(), ["fragrance"]) is None


def test_saved_avoidance_adds_no_unknown_marks_elsewhere():
    assert _service()._attribute_checks("p", _intent(), ["fragrance"]) == {}
    assert _service()._attribute_checks("p", _intent(["fragrance"])) == {"fragrance": "unknown"}
