"""Collections: broad requests that span several types, and accessories for devices not sold."""

from contextlib import closing
import sqlite3

import pytest

from catalog_config import AMAZON_DB_PATH
from search import catalog_understanding as cu
from search.query_understanding import ParsedQuery, expand_sub_categories


def _vocabulary(**overrides):
    base = dict(
        categories=("Electronics",),
        labels={"Phone Basic Cases": "Electronics", "Wall Chargers": "Electronics"},
        groups={"Phone Accessories": ("Phone Basic Cases", "Wall Chargers")},
        audiences={},
        patterns={cu._OUTSIDE: cu._compile(["iphones?", "yoga mats?"])},
        brands={},
        stand_ins={"Phone Accessories": cu._compile(["smartphones?", "iphones?"])},
    )
    return cu.CatalogVocabulary(**{**base, **overrides})


def test_a_device_not_sold_searches_its_accessories():
    parsed = cu._accessories_instead(
        ParsedQuery(original_query="I need an iPhone", unavailable_terms=["iPhone"]), _vocabulary(),
    )
    assert parsed.sub_category == "Phone Accessories"
    assert parsed.unavailable_terms == []
    assert parsed.instead_of == "iPhone"
    assert parsed.compatible_with == ["iphone"]


def test_other_missing_products_stay_unavailable():
    parsed = cu._accessories_instead(
        ParsedQuery(original_query="a yoga mat", unavailable_terms=["yoga mat"]), _vocabulary(),
    )
    assert parsed.sub_category is None and parsed.unavailable_terms == ["yoga mat"]
    assert parsed.instead_of is None


def test_rules_fallback_also_finds_accessories():
    parsed = cu._accessories_instead(cu.parse_rules("iphone", _vocabulary()), _vocabulary())
    assert parsed.sub_category == "Phone Accessories" and parsed.instead_of == "iphone"


catalog = pytest.mark.skipif(not AMAZON_DB_PATH.exists(), reason="catalog database not built")


@catalog
@pytest.mark.parametrize("query", ["sports clothing", "workout clothes", "athletic wear", "gym gear"])
def test_broad_sports_requests_map_to_activewear(query):
    with closing(sqlite3.connect(AMAZON_DB_PATH)) as conn:
        vocabulary = cu.load_vocabulary(conn)
    assert cu.parse_rules(query, vocabulary).sub_category == "Activewear"


@catalog
def test_collections_expand_for_the_vector_filter():
    assert set(expand_sub_categories(["Activewear"])) == {
        "Women's Active Shorts", "Men's Athletic Socks", "Men's Road Running Shoes",
    }


@catalog
def test_sports_socks_keep_their_own_type():
    with closing(sqlite3.connect(AMAZON_DB_PATH)) as conn:
        vocabulary = cu.load_vocabulary(conn)
    assert cu.parse_rules("sports socks", vocabulary).sub_category == "Men's Athletic Socks"


@catalog
@pytest.mark.parametrize("query", ["christmas decorations", "xmas ornaments", "holiday lights"])
def test_holiday_requests_map_to_ornaments_and_lights(query):
    with closing(sqlite3.connect(AMAZON_DB_PATH)) as conn:
        vocabulary = cu.load_vocabulary(conn)
    assert cu.parse_rules(query, vocabulary).sub_category == "Holiday Decorations"


@catalog
def test_a_plain_phone_searches_phone_accessories():
    with closing(sqlite3.connect(AMAZON_DB_PATH)) as conn:
        vocabulary = cu.load_vocabulary(conn)
    parsed = cu._accessories_instead(ParsedQuery(original_query="I need a phone", unavailable_terms=["phone"]), vocabulary)
    assert parsed.sub_category == "Phone Accessories" and parsed.instead_of == "phone"
