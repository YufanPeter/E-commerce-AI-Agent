"""English parsing without a model: rules, validation of model output, and the fallback."""

from dataclasses import replace
import re
from unittest.mock import patch

import pytest

from search import catalog_understanding as cu
from search.query_understanding import ParsedQuery


def _pattern(*terms):
    return cu._compile(list(terms))


VOCABULARY = cu.CatalogVocabulary(
    categories=("Electronics", "Clothing & Sports"),
    labels={
        "Smartwatches": "Electronics", "Smartwatch Bands": "Electronics", "Earbud Headphones": "Electronics",
        "Over-Ear Headphones": "Electronics", "Men's Road Running Shoes": "Clothing & Sports",
    },
    groups={"Headphones": ("Earbud Headphones", "Over-Ear Headphones")},
    audiences={"Men's Road Running Shoes": "men"},
    patterns={
        "Headphones": _pattern("headphones?"),
        "Smartwatches": _pattern(r"smart ?watch(?:es)?"),
        "Smartwatch Bands": _pattern(r"smart ?watch bands?", "watch ?bands?"),
        "Earbud Headphones": _pattern("earbuds?"),
        "Over-Ear Headphones": _pattern("over-ear headphones?"),
        "Men's Road Running Shoes": _pattern("running shoes?"),
        cu._OUTSIDE: _pattern("yoga mats?", "iphones?"),
    },
    brands={"sony": "Sony", "bose": "Bose"},
)


def test_rules_read_type_budget_exclusions_and_brand():
    parsed = cu.parse_rules("Sony headphones under $80, no watch bands, without alcohol", VOCABULARY)
    assert parsed.sub_category == "Headphones"
    assert parsed.max_price == 80 and parsed.brand_include == ["Sony"]
    assert parsed.sub_category_exclude == ["Smartwatch Bands"] and parsed.excluded_attributes == ["alcohol"]


@pytest.mark.parametrize("query, minimum, maximum", [
    ("Bluetooth speaker with at least 20 hours of battery life", None, None),
    ("over 100W charger under $30", None, 30),
    ("power bank over 20,000mAh under 40", None, 40),
    ("at least 30 SPF sunscreen under $20", None, 20),
    ("headphones over $50", 50, None),
])
def test_measurements_are_not_budgets(query, minimum, maximum):
    parsed = cu.parse_rules(query, VOCABULARY)
    assert (parsed.min_price, parsed.max_price) == (minimum, maximum)


AROUND_50 = (37.5, 62.5, 50)


@pytest.mark.parametrize("query, expected", [
    ("moisturizer around $50", AROUND_50),
    ("moisturizer for about 50 bucks", AROUND_50),
    ("a $50 moisturizer", AROUND_50),
    ("moisturizer, budget around 50", AROUND_50),
    ("moisturizer ~$50", AROUND_50),
    ("$50-ish moisturizer", AROUND_50),
    ("moisturizer $40-60", (40, 60, None)),
    ("moisturizer $40 to $60", (40, 60, None)),
    ("moisturizer 40-60 dollars", (40, 60, None)),
    ("moisturizer under $50", (None, 50, None)),
    ("speaker with 20-30 hours of battery", (None, None, None)),
    ("speaker with around 20 hours of battery", (None, None, None)),
])
def test_rules_tell_targets_from_limits(query, expected):
    parsed = cu.parse_rules(query, VOCABULARY)
    assert (parsed.min_price, parsed.max_price, parsed.price_target) == expected


@pytest.mark.parametrize("query, soft_terms, excluded, features", [
    # The model read no wanted property; the word in the message must not bring one back.
    ("speaker, I don't need it to be waterproof", [], [], []),
    ("silicone-free shampoo", [], ["silicone"], []),
    ("silicone-free shampoo", ["silicone-free"], ["silicone"], []),
    ("waterproof speaker", ["waterproof"], [], ["waterproof"]),
    ("ANC earbuds", ["active noise cancellation"], [], ["noise_cancelling"]),
])
def test_model_features_come_from_what_it_read_as_wanted(query, soft_terms, excluded, features):
    parsed = cu._from_model(query, {"soft_terms": soft_terms, "excluded_attributes": excluded}, VOCABULARY)
    assert parsed.features == features


def test_model_aspects_count_as_wanted_features():
    vocabulary = cu.CatalogVocabulary(**{**VOCABULARY.__dict__, "aspects": ("noise_cancellation", "comfort")})
    parsed = cu._from_model("noise cancelling earbuds", {"aspects": ["noise_cancellation", "comfort"]}, vocabulary)
    assert parsed.features == ["noise_cancelling"]


@pytest.mark.parametrize("query, features", [
    ("silicone-free shampoo", []),
    ("leather free wallet", []),
    ("speaker, I don't need it to be waterproof", []),
    ("speaker, waterproof isn't important", []),
    ("waterproof leather boots", ["leather", "waterproof"]),
])
def test_rule_fallback_skips_mentions_that_do_not_ask_for_a_value(query, features):
    assert sorted(cu.parse_rules(query, VOCABULARY).features) == features


def test_model_sees_the_conditions_it_may_take_back():
    limit = {"attribute": "battery_hours", "op": ">=", "value": 20.0}
    base = ParsedQuery("speakers", retrieval_query="speakers", features=["waterproof"], excluded_attributes=["bpa"], numeric=[limit])
    with patch.object(cu, "_call_model", return_value='{"withdrawn": ["no bpa"]}') as call:
        parsed = cu.understand("BPA is ok", VOCABULARY, base=base)
    assert call.call_args.args[3].endswith("Conditions in force: waterproof; no bpa; battery_hours >= 20")
    assert parsed.withdrawn == ["no bpa"]


def test_model_target_becomes_a_window_even_when_copied_into_a_limit():
    for extra in ({}, {"max_price": 50}):
        parsed = cu._from_model("moisturizer around $50", {"price_target": 50, **extra}, VOCABULARY)
        assert (parsed.min_price, parsed.max_price, parsed.price_target) == AROUND_50
    ranged = cu._from_model("moisturizer $40-60", {"min_price": 40, "max_price": 60}, VOCABULARY)
    assert (ranged.min_price, ranged.max_price, ranged.price_target) == (40, 60, None)
    invented = cu._from_model("moisturizer around $50", {"price_target": 45}, VOCABULARY)
    assert (invented.min_price, invented.max_price, invented.price_target) == (None, None, None)


def test_target_window_is_replaced_whole_and_kept_otherwise():
    base = ParsedQuery("moisturizer around $50", sub_category="Smartwatches", min_price=37.5, max_price=62.5, price_target=50)
    cheaper = cu.merge_turn(ParsedQuery("Under $40", max_price=40.0), base, {})
    assert (cheaper.min_price, cheaper.max_price, cheaper.price_target) == (None, 40, None)
    kept = cu.merge_turn(ParsedQuery("with GPS"), base, {})
    assert (kept.min_price, kept.max_price, kept.price_target) == AROUND_50
    assert base.without(["budget"]).price_target is None
    assert cu.semantic_query(cu.parse_rules("moisturizer around $50", VOCABULARY)) == "moisturizer"


def test_model_budget_must_not_be_a_measurement():
    assert cu._stated_price(20, "speaker with at least 20 hours of battery") is None
    assert cu._stated_price(30, "100W charger under 30") == 30


def test_device_name_beside_a_catalog_type_is_not_out_of_catalog():
    assert cu.parse_rules("iPhone earbuds", VOCABULARY).unavailable_terms == []
    assert cu.parse_rules("Find me a yoga mat", VOCABULARY).unavailable_terms == ["yoga mat"]


def test_model_output_is_verified_against_the_query():
    arguments = {
        "product_type": "Invented Type", "brand_include": ["Sony", "Bose"], "brand_exclude": [],
        "max_price": 15, "min_price": 3, "excluded_attributes": ["alcohol", "fragrance"],
        "soft_terms": [], "unavailable_terms": [], "product_type_exclude": ["Nope"], "category_exclude": [],
        "needs_clarification": False,
    }
    parsed = cu._from_model("Sony iPhone 15 case without alcohol, up to $3", arguments, VOCABULARY)
    assert parsed.sub_category is None and parsed.sub_category_exclude == []
    assert parsed.brand_include == ["Sony"]  # Bose was never said.
    assert parsed.max_price == 15 and parsed.min_price == 3  # 15 is present; see the next assertion.
    assert cu._stated_price(80, "iPhone 15 case") is None
    assert parsed.excluded_attributes == ["alcohol"]


def test_audience_gap_reports_the_missing_leaf():
    parsed = cu._audience_gap(ParsedQuery("Women's running shoes", sub_category="Men's Road Running Shoes"), VOCABULARY)
    assert parsed.unavailable_terms == ["Women's Road Running Shoes"]


def test_model_failure_falls_back_to_rules():
    with patch.object(cu, "_call_model", side_effect=TimeoutError("down")):
        parsed = cu.understand("smartwatches under $50", VOCABULARY)
    assert parsed.sub_category == "Smartwatches" and parsed.max_price == 50


def test_budget_follow_up_keeps_previous_type():
    base = ParsedQuery("smartwatches", sub_category="Smartwatches", retrieval_query="smartwatches")
    current = ParsedQuery("Under $13", max_price=13.0, retrieval_query="Under $13", needs_clarification=True)
    merged = cu.merge_turn(current, base, {})
    assert merged.sub_category == "Smartwatches" and merged.max_price == 13 and not merged.needs_clarification
    assert re.search("smartwatches", merged.retrieval_query)


def test_two_letter_brands_count_only_in_capitals():
    vocabulary = replace(VOCABULARY, brands={**VOCABULARY.brands, "hp": "HP", "on": "ON"})
    assert cu.parse_rules("HP headphones", vocabulary).brand_include == ["HP"]
    assert cu.parse_rules("turn on headphones", vocabulary).brand_include == []
    assert cu._from_model("HP earbuds", {"brand_include": ["HP"]}, vocabulary).brand_include == ["HP"]
    assert cu._from_model("put on earbuds", {"brand_include": ["ON"]}, vocabulary).brand_include == []
