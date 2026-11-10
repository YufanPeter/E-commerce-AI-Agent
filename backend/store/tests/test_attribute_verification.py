import sqlite3

import pytest

from graph.build_graph import attribute_rows, rejection_rows, verified_claims
from graph.extract import Product
from store.attribute_rules import extract_attributes
from store.attribute_verification import (
    DEFINITIONS, AttributeVerifier, IncompleteVerification, candidate, verify_catalog,
)
from store.import_amazon import SCHEMA
from store.import_product_data import DEFAULT_INIT_SQL, init_database


def _values(rows, attribute):
    return [row["value"] for row in rows if row["attribute"] == attribute]


@pytest.mark.parametrize("text, attribute, value", [
    ("Built-in 13,400mAh battery", "battery_mah", "13400.0"),
    ("A 1,800W power strip", "wattage", "1800.0"),
])
def test_thousands_separators_keep_the_whole_number(text, attribute, value):
    assert _values(extract_attributes("Electronics", text, ""), attribute) == [value]


def test_a_leading_decimal_point_is_part_of_the_number():
    assert _values(extract_attributes("Beauty & Personal Care", "Face Stick | .45 oz", ""), "volume_ml") == ["13.3"]


def test_screen_size_is_not_an_iphone_model():
    devices = _values(extract_attributes("Electronics", "Leather case for iPhone 6.1-inch", ""), "compatible_device")
    assert devices == ["iphone"]


def test_every_extracted_attribute_has_a_definition():
    rows = extract_attributes("Electronics", "Bluetooth 20W 32GB IPX7 iPhone 14 black waterproof 3 ft 500mAh 8 hours battery", "")
    assert {row["attribute"] for row in rows} <= set(DEFINITIONS)


def _item(value="1"):
    return candidate("wattage", value, "Chargers", "A charger", "A 20W charger", 2, 5)


def test_verdicts_are_cached_by_content(tmp_path):
    calls = []

    def ask(attribute, batch):
        calls.append(len(batch))
        return ["yes"] * len(batch)

    path = tmp_path / "verdicts.jsonl"
    items = [_item("1"), _item("2")]
    assert AttributeVerifier(path, ask).verdicts(items)[items[0]] == "yes"
    AttributeVerifier(path, ask).verdicts(items)
    assert calls == [2]


def test_a_failed_batch_blocks_publishing_and_can_be_resumed(tmp_path):
    path = tmp_path / "verdicts.jsonl"

    def broken(attribute, batch):
        raise OSError("down")

    with pytest.raises(IncompleteVerification):
        AttributeVerifier(path, broken).verdicts([_item()])
    assert AttributeVerifier(path, lambda a, b: ["no"] * len(b)).verdicts([_item()])[_item()] == "no"


def _catalog():
    conn = sqlite3.connect(":memory:")
    init_database(conn, DEFAULT_INIT_SQL)
    init_database(conn, SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def _add(conn, *rows):
    for row in rows:
        conn.execute("INSERT INTO product_attributes VALUES ('p', ?, ?, ?, 'title', 0, 5, 'rule')", row)


class _Verdicts:
    def __init__(self, by_value):
        self.by_value = by_value

    def verdicts(self, items):
        return {item: self.by_value[item.value] for item in items}


def _published(conn):
    return conn.execute("SELECT attribute, value, status FROM product_attributes ORDER BY value").fetchall()


def _with_product(conn):
    # verify_catalog joins products; only the columns it reads matter here.
    conn.execute("DROP VIEW IF EXISTS product_price_ranges")
    conn.execute("ALTER TABLE products RENAME TO products_real")
    conn.execute("CREATE TABLE products (product_id TEXT, title TEXT, sub_category TEXT)")
    conn.execute("INSERT INTO products VALUES ('p', 'Listing title here', 'Shampoos')")


def test_refused_values_leave_the_catalog_but_stay_auditable():
    conn = _catalog()
    _with_product(conn)
    _add(conn, ("contains", "paraben", "observed"), ("free_of", "sulfate", "observed"))
    verify_catalog(conn, _Verdicts({"paraben": "no", "sulfate": "yes"}))
    assert _published(conn) == [("free_of", "sulfate", "observed")]
    assert conn.execute("SELECT attribute, value, verdict FROM attribute_rejections").fetchall() == [("contains", "paraben", "no")]


def test_one_surviving_conflicting_value_is_no_longer_ambiguous():
    conn = _catalog()
    _with_product(conn)
    _add(conn, ("battery_hours", "2.0", "ambiguous"), ("battery_hours", "16.0", "ambiguous"))
    verify_catalog(conn, _Verdicts({"2.0": "no", "16.0": "yes"}))
    assert _published(conn) == [("battery_hours", "16.0", "observed")]


def test_unsettled_values_are_withheld_and_conflicts_stay_ambiguous():
    conn = _catalog()
    _with_product(conn)
    _add(conn, ("wattage", "20.0", "ambiguous"), ("wattage", "30.0", "ambiguous"), ("spf", "50.0", "observed"))
    verify_catalog(conn, _Verdicts({"20.0": "yes", "30.0": "yes", "50.0": "unclear"}))
    assert [row[2] for row in _published(conn)] == ["ambiguous", "ambiguous"]
    assert conn.execute("SELECT verdict FROM attribute_rejections").fetchall() == [("unclear",)]


class _ByQuote:
    def verdicts(self, items):
        return {item: "no" if "no sulfates" in item.context else "yes" for item in items}


def test_refused_model_claims_are_kept_out_of_graph_and_attributes():
    product = Product("p", "Shampoos", "Beauty", "Sulfate shampoo with no sulfates and aloe", "", (("r1", 5, "No sulfates here"),))
    products = {"p": product}
    claims = [
        {"product_id": "p", "relation": "CONTAINS", "value": "sulfate", "source": "title", "quote": "no sulfates"},
        {"product_id": "p", "relation": "PRAISED_FOR", "value": "scent", "source": "r1", "quote": "No sulfates here"},
    ]
    kept, refused = verified_claims(claims, products, _ByQuote())
    assert [claim["relation"] for claim in kept] == ["PRAISED_FOR"]  # Review claims are not listing facts.
    assert attribute_rows(kept, products) == []
    assert rejection_rows(refused, products) == [("p", "contains", "sulfate", "no", "title", 21, 32, "llm")]


def test_model_free_of_rows_are_never_stated_as_confirmed():
    products = {"p": Product("p", "Shampoos", "Beauty", "Sulfate free shampoo with aloe", "", ())}
    claims = [
        {"product_id": "p", "relation": "FREE_OF", "value": "sulfate", "source": "title", "quote": "Sulfate free"},
        {"product_id": "p", "relation": "CONTAINS", "value": "aloe_vera", "source": "title", "quote": "aloe"},
    ]
    assert {row[1]: row[3] for row in attribute_rows(claims, products)} == {"free_of": "ambiguous", "contains": "observed"}
