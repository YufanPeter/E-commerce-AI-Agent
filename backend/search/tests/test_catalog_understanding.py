"""English parsing without a model: rules, validation of model output, and the fallback."""

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
