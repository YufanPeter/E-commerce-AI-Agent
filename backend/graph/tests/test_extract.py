"""Claim validation: a claim needs an allowed value, an allowed source, and real support."""

from datetime import datetime, timedelta, timezone

import pytest

from graph.common import is_peak, seconds_until_off_peak
from graph.extract import Product, _spend_cny, load_ontology, validate_claim


ONTOLOGY = load_ontology()
PRODUCT = Product(
    "p1", "Men's Wallets", "Clothing & Sports",
    "Slim Genuine Leather Wallet", "Zipper closure. Fake leather lining. 1-Pack. Machine Wash.",
    (("r1", 5, "The waist is a bit big but the leather is nice."), ("r2", 1, "It fit fine but fell apart.")),
)


def _reason(relation, value, source, quote):
    return validate_claim({"relation": relation, "value": value, "source": source, "quote": quote}, PRODUCT, ONTOLOGY)[1]


@pytest.mark.parametrize("claim,reason", [
    (("MADE_OF", "genuine_leather", "title", "Genuine Leather"), ""),
    (("HAS_ATTRIBUTE", "machine_washable", "description", "machine wash"), ""),  # Case-insensitive quote.
    (("MADE_OF", "plutonium", "title", "Genuine Leather"), "value_not_in_ontology"),
    (("MADE_OF", "leather", "title", "Made of the best leather"), "quote_not_found"),
    (("HAS_ATTRIBUTE", "button_closure", "description", "Zipper closure"), "value_not_in_quote"),
    (("MADE_OF", "leather", "description", "Fake leather lining"), "imitation_material"),
    (("HAS_ATTRIBUTE", "multi_pack", "description", "1-Pack"), "single_unit"),
    (("MADE_OF", "leather", "r1", "the leather is nice"), "source_not_allowed"),
    (("PRAISED_FOR", "fit", "title", "Slim"), "source_not_allowed"),
    (("PRAISED_FOR", "fit", "r2", "It fit fine"), "praise_in_low_rating"),
    (("CRITICIZED_FOR", "fit", "r1", "The waist is a bit big"), ""),  # Mixed high-star reviews count.
    (("CRITICIZED_FOR", "fit", "r9", "The waist"), "unknown_source"),
])
def test_validate_claim(claim, reason):
    assert _reason(*claim) == reason


def test_free_of_needs_a_negation_and_devices_are_normalized():
    beauty = Product("p2", "Shampoos", "Beauty & Personal Care", "Sulfate Free Shampoo", "Contains sulfate.", ())
    check = lambda relation, quote: validate_claim(
        {"relation": relation, "value": "sulfate", "source": "title" if "Free" in quote else "description", "quote": quote},
        beauty, ONTOLOGY,
    )[1]
    assert check("FREE_OF", "Sulfate Free") == ""
    assert check("CONTAINS", "Sulfate Free") == "negation_conflict"
    assert check("FREE_OF", "Contains sulfate") == "negation_conflict"
    phone = Product("p3", "Phone Basic Cases", "Electronics", "Case for iPhone 14 Pro", "", ())
    normalized, _ = validate_claim({"relation": "COMPATIBLE_WITH", "value": "iPhone 14 Pro", "source": "title", "quote": "iPhone 14 Pro"}, phone, ONTOLOGY)
    assert normalized["value"] == "iphone_14_pro"


def test_spend_uses_official_token_prices():
    record = {"prompt_tokens": 2_000_000, "cached_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert _spend_cny(record, 1.0) == pytest.approx(5.02)
    assert _spend_cny(record, 2.0) == pytest.approx(10.04)


def test_peak_windows_use_beijing_time():
    beijing = timezone(timedelta(hours=8))
    tuesday = datetime(2026, 11, 10, tzinfo=beijing)
    assert is_peak(tuesday.replace(hour=10, minute=30))
    assert seconds_until_off_peak(tuesday.replace(hour=10, minute=30)) == 90 * 60
    assert not is_peak(tuesday.replace(hour=12, minute=30))
    assert not is_peak(tuesday.replace(hour=18))
    assert not is_peak(datetime(2026, 11, 14, 10, tzinfo=beijing))  # Saturday.
