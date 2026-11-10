from types import SimpleNamespace

import numpy as np
import pytest

from agent.tools.knowledge import KnowledgeTool, _guide_text
from graph.build_graph import attribute_rows, own_device, similar_pairs
from graph.extract import Product
from graph.graph_store import AccessoryMatches, GraphQuery, Neo4jGraphStore, wanted_terms
from graph.summarize import facets, validated_guide
from search.catalog_understanding import _devices_named, _named_values


def _session(hits=None):
    return SimpleNamespace(
        recall_hits=lambda: hits or [], recall_parsed=lambda: {}, task=SimpleNamespace(focus_product_id=None),
        remember_search=lambda *args, **kwargs: None,
    )


def test_similar_pairs_stay_in_group_and_skip_duplicates():
    ids = ["a", "b", "c", "d"]
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [0.9, 0.1], [1.0, 0.0]], dtype=np.float32)
    groups = {"a": "x", "b": "x", "c": "x", "d": "y"}
    pairs = similar_pairs(ids, vectors, groups, {"a": "same", "b": "same"})
    assert {(p["a"], p["b"]) for p in pairs} == {("a", "c"), ("b", "c"), ("c", "a"), ("c", "b")}


def test_own_device_is_not_the_device_a_listing_works_with():
    assert own_device("Apple Watch Series 8 for iPhone") == "apple_watch"
    assert own_device("Smart Watch, Fitness Tracker for iPhone Android") is None
    assert own_device("Generic Smart Watch") is None


def test_attribute_rows_locate_the_quote_and_skip_reviews():
    product = Product("p", "L", "C", "Soft Leather Band", "", (("r1", 5, "Leather smells nice"),))
    claims = [
        {"product_id": "p", "relation": "MADE_OF", "value": "leather", "source": "title", "quote": "leather"},
        {"product_id": "p", "relation": "PRAISED_FOR", "value": "scent", "source": "r1", "quote": "smells nice"},
        {"product_id": "p", "relation": "MADE_OF", "value": "cotton", "source": "title", "quote": "cotton"},
    ]
    assert attribute_rows(claims, {"p": product}) == [("p", "material", "leather", "observed", "title", 5, 12, "llm")]


def test_wanted_terms_penalize_criticism_and_widen_iphone_models():
    wanted = wanted_terms(GraphQuery(aspects=("comfort",), devices=("iphone_15",)))
    assert {"key": "aspect:comfort", "relation": "CRITICIZED_FOR", "weight": -1.0} in wanted
    assert {"key": "device:iphone", "relation": "COMPATIBLE_WITH", "weight": 1.0} in wanted


def test_store_goes_quiet_after_an_outage():
    calls = []

    class Down:
        def execute_query(self, *args, **kwargs):
            calls.append(1)
            raise OSError("connection refused")

    store = Neo4jGraphStore(Down())
    assert store.similar("p", 5) == []
    assert store.recall(GraphQuery(aspects=("comfort",)), 5) == []
    assert store.guide("Smartwatches") is None
    assert store.accessories("p", 5) == AccessoryMatches()
    assert len(calls) == 1


def test_store_stays_off_for_another_catalog_version():
    class Stale:
        def execute_query(self, query, parameters_):
            return [SimpleNamespace(data=lambda: {"digest": "old"})], None, None

    store = Neo4jGraphStore(Stale(), expected_digest="new")
    assert store.similar("p", 5) == []
    assert store._quiet_until == float("inf")


def test_guides_only_keep_supplied_facts():
    facts = facets([{"relation": "PRAISED_FOR", "value": "comfort", "mentions": 4}])
    guide = validated_guide({
        "summary": "Comfort matters.",
        "look_for": [{"value": "comfort", "advice": "Check the fit."}, {"value": "invented", "advice": "Nope."}],
        "watch_out": [], "clarify": ["Budget?", "Use?", "Size?", "Fourth?"],
    }, facts)
    assert [item["value"] for item in guide["look_for"]] == ["comfort"]
    assert len(guide["clarify"]) == 3
    assert "- Comfort: Check the fit." in _guide_text(guide)


def test_query_signals_come_from_the_ontology_and_device_rules():
    assert _named_values("comfortable earbuds with good battery life", ("battery_life", "comfort")) == ["battery_life"]
    assert "iphone_15" in _devices_named("case for iPhone 15")


class _Recommend:
    def __init__(self, service):
        self.service = service
        self.searched = []

    def _get_service(self):
        return self.service

    def _attach_price_displays(self, cards, parsed):
        pass

    def run(self, query, session, slots):
        self.searched.append(query)
        return SimpleNamespace(tool_name="recommend", payload={})


def _hit(product_id, price):
    return SimpleNamespace(product_id=product_id, title=product_id, brand="B", category="C", sub_category="L", min_price=price, max_price=price, sku_count=1, evidence=[], attribute_checks={}, graph_evidence=())


def _service(graph, prices):
    def hits_for(ids, max_price=None, popular_first=False):
        return [_hit(i, prices[i]) for i in ids if i in prices and (max_price is None or prices[i] <= max_price)]

    return SimpleNamespace(graph=graph, min_price=lambda i: prices.get(i), hits_for=hits_for)


def test_similar_but_cheaper_prices_come_from_the_catalog(monkeypatch):
    monkeypatch.setattr("agent.tools.knowledge._card_with_evidence", lambda hit, query: {"product_id": hit.product_id})
    graph = SimpleNamespace(similar=lambda pid, limit: ["dear", "cheap"])
    tool = KnowledgeTool("similar", _Recommend(_service(graph, {"focus": 50.0, "dear": 80.0, "cheap": 20.0})))
    result = tool.run("Something similar but cheaper", _session([{"product_id": "focus", "title": "Watch"}]), {"cheaper": True})
    assert [card["product_id"] for card in result.payload["products"]] == ["cheap"]


def test_similar_searches_normally_when_the_graph_is_down():
    recommend = _Recommend(_service(None, {"focus": 50.0}))
    session = _session([{"product_id": "focus", "title": "Watch"}])
    session.recall_parsed = lambda: {"sub_category": "Smartwatches"}
    KnowledgeTool("similar", recommend).run("Something similar but cheaper", session, {"cheaper": True})
    assert recommend.searched == ["Smartwatches under $49.99"]


def test_unconfirmed_accessories_say_so(monkeypatch):
    monkeypatch.setattr("agent.tools.knowledge._card_with_evidence", lambda hit, query: {"product_id": hit.product_id})
    graph = SimpleNamespace(accessories=lambda pid, limit: AccessoryMatches(("band",), None, False))
    tool = KnowledgeTool("accessories", _Recommend(_service(graph, {"band": 9.0})))
    result = tool.run("What bands fit this watch?", _session([{"product_id": "w", "title": "Watch"}]), {})
    assert "don't confirm" in result.payload["opening"]


def test_follow_ups_refine_the_accessories_on_screen(monkeypatch):
    monkeypatch.setattr("agent.tools.knowledge._card_with_evidence", lambda hit, query: {"product_id": hit.product_id})
    graph = SimpleNamespace(accessories=lambda pid, limit: AccessoryMatches(("band",), None, False))
    session = _session([{"product_id": "w", "title": "Watch"}])
    remembered = []
    session.remember_search = lambda parsed, hits, **kwargs: remembered.append(parsed)
    KnowledgeTool("accessories", _Recommend(_service(graph, {"band": 9.0}))).run("What bands fit this watch?", session, {})
    assert remembered[0]["sub_category"] == "L"  # "Cheaper ones" now means cheaper bands, not cheaper watches.
