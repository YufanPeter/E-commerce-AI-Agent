"""Empty searches: conditions carried from earlier turns, and device models no listing names."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import sqlite3
from types import SimpleNamespace

from agent.session import AgentSession
from agent.tools import recommend as rec
from agent.tools.recommend import RecommendTool
from search.query_understanding import ParsedQuery
from search.search_service import SearchResult


def _hit(pid: str) -> SimpleNamespace:
    return SimpleNamespace(
        product_id=pid, title=f"Case {pid}", brand=f"B{pid}", category="Electronics", sub_category="Phone Basic Cases",
        base_price=10.0, score=1.0, best_distance=None, rerank_score=None, evidence=[], min_price=10.0,
        max_price=10.0, sku_count=1, attribute_checks={}, graph_evidence=(), rating=None,
        to_dict=lambda: {"product_id": pid},
    )


@dataclass
class _Service:
    """Empty while an earlier device condition is carried; finds cases for this turn alone."""
    own_type: str | None = "Phone Cases"

    def __post_init__(self):
        self.calls: list[ParsedQuery | None] = []

    def search(self, query, top_k_chunks=50, top_k_products=10, base=None, **_):
        self.calls.append(base)
        if base is not None:
            parsed = ParsedQuery(original_query=query, sub_category="Phone Cases", compatible_with=["iphone_15"])
            return SearchResult(parsed, [], 0, 0)
        parsed = ParsedQuery(original_query=query, sub_category=self.own_type)
        return SearchResult(parsed, [_hit("p1"), _hit("p2")] if self.own_type else [], 2, 2)


def _tool(service, db_path=None) -> RecommendTool:
    store = SimpleNamespace(db_path=db_path or ":memory:", get_products_by_ids=lambda ids: [])
    return RecommendTool(search_service=service, decomposer=lambda q: [], product_store=store)


def test_carried_condition_is_dropped_when_nothing_matches():
    base = ParsedQuery(original_query="cases for the iPhone 15", sub_category="Phone Cases", compatible_with=["iphone_15"])
    service = _Service()
    result = _tool(service).run("Show all phone cases", AgentSession(), {"base_parsed": base})
    assert [p["product_id"] for p in result.payload["products"]] == ["p1", "p2"]
    assert result.payload["summary"]["relaxed"] == ["works with iPhone 15"]
    assert service.calls[-1] is None


def test_a_turn_without_its_own_type_is_not_widened():
    base = ParsedQuery(original_query="phone cases", sub_category="Phone Cases", compatible_with=["iphone_15"])
    result = _tool(_Service(own_type=None)).run("under $3", AgentSession(), {"base_parsed": base})
    assert result.payload["products"] == []
    assert result.payload["summary"]["relaxed"] == []


def test_models_are_listed_newest_first(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE products (product_id TEXT, sub_category TEXT)")
        conn.execute("CREATE TABLE product_attributes (product_id TEXT, attribute TEXT, value TEXT)")
        for i, value in enumerate(["iphone_8", "iphone_14", "iphone_14_pro_max", "iphone_12", "samsung", "iphone"]):
            conn.execute("INSERT INTO products VALUES (?, 'Phone Basic Cases')", (f"p{i}",))
            conn.execute("INSERT INTO product_attributes VALUES (?, 'compatible_device', ?)", (f"p{i}", value))
    parsed = ParsedQuery(original_query="cases for iPhone 15", sub_category="Phone Basic Cases",
                         compatible_with=["iphone_15", "iphone"])
    named, models = _tool(_Service(), db)._device_models(parsed)
    assert named == "iPhone 15"
    assert models == ["iPhone 14 Pro Max", "iPhone 14", "iPhone 12", "iPhone 8"]


def test_device_names_read_naturally():
    assert rec._device_name("iphone_14_pro_max") == "iPhone 14 Pro Max"
    assert rec._device_name("samsung") == "Samsung"


def test_model_spellings_are_unified():
    assert rec._normalized_device("iphone_12pro_max") == "iphone_12_pro_max"
    assert rec._device_name(rec._normalized_device("iphone_12pro_max")) == "iPhone 12 Pro Max"


def test_models_come_from_cases_when_a_search_mixes_types(tmp_path, monkeypatch):
    db = tmp_path / "catalog.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE products (product_id TEXT, sub_category TEXT)")
        conn.execute("CREATE TABLE product_attributes (product_id TEXT, attribute TEXT, value TEXT)")
        for pid, label, value in [("c1", "Phone Basic Cases", "iphone_14"), ("w1", "Wall Chargers", "iphone_15")]:
            conn.execute("INSERT INTO products VALUES (?, ?)", (pid, label))
            conn.execute("INSERT INTO product_attributes VALUES (?, 'compatible_device', ?)", (pid, value))
    monkeypatch.setattr(rec, "expand_sub_categories", lambda names: ["Phone Basic Cases", "Wall Chargers"])
    parsed = ParsedQuery(original_query="iPhone", sub_category="Phone Accessories", compatible_with=["iphone"])
    assert _tool(_Service(), db)._device_models(parsed) == ("", ["iPhone 14"])
