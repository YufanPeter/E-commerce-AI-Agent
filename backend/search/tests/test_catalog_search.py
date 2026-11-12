"""Hybrid catalog retrieval with fake indexes: SQLite stays the source of truth."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from graph.graph_store import GraphHit
from rag.retriever import RetrievedChunk
from search import catalog_understanding
from search.catalog_search import CatalogSearchService
from store.import_amazon import build_amazon_catalog
from store.product_store import ProductStore


CELLS = {
    "Smartwatches": "Electronics > Wearable Technology > Smartwatches",
    "Smartwatch Bands": "Cell Phones & Accessories > Accessories > Smartwatch Accessories > Smartwatch Bands",
    "Phone Basic Cases": "Cell Phones & Accessories > Cases, Holsters & Sleeves > Basic Cases",
    "Lightning Cables": "Electronics > Computers & Accessories > Computer Accessories & Peripherals > Cables & Accessories > Cables & Interconnects > Lightning Cables",
}
SAMPLES = [
    ("B000000001", "Atlas Smartwatch", "Atlas", 25, "Smartwatches", "A rugged watch for hiking."),
    ("B000000002", "Summit Smartwatch", "Summit", 42, "Smartwatches", "A slim watch for the office."),
    ("B000000003", "Apex Smartwatch", "Apex", 65, "Smartwatches", "A bright watch for runners."),
    ("B000000004", "Atlas Replacement Band", "Atlas", 8, "Smartwatch Bands", "A soft strap."),
    ("B000000005", "Vector Smart Watch Bands 20mm", "Vector", 30, "Smartwatches", "Straps in a mislabeled leaf."),
    ("B000000006", "Shield Slim Case for iPhone 15", "Shield", 12, "Phone Basic Cases", "A slim fit case."),
    ("B000000007", "Guard Phone Case for iPhone 13 and iPhone 14", "Guard", 10, "Phone Basic Cases", "Fits two models."),
    ("B000000008", "Flex Phone Case", "Flex", 9, "Phone Basic Cases", "A stretchy case for most smartphones."),
    ("B000000009", "Duo Phone Case for iPhone 14/15", "Duo", 11, "Phone Basic Cases", "One mold, two phones."),
    ("B000000010", "Tilt Phone Case for iPhone 15 Pro Max", "Tilt", 14, "Phone Basic Cases", "Made for the largest model."),
    ("B000000011", "Clear Phone Case for Pixel 7", "Clear", 8, "Phone Basic Cases", "A clear case."),
    ("B000000012", "Snug Phone Case for iPhone 15 Pro", "Snug", 13, "Phone Basic Cases", "Does NOT fit iPhone 15. Only for the Pro."),
    ("B000000013", "Grip Phone Case for iPhone 15 Pro Max", "Grip", 12, "Phone Basic Cases", "This iPhone 15 phone case is soft."),
    ("B000000014", "Fold Phone Case", "Fold", 12, "Phone Basic Cases",
     'Fits iPhone 15 Pro. NOT fit iPhone 15 Mini (5.4"), iPhone 15 (6.1") or the Plus.'),
    ("B000000015", "Volt iPhone Charger Cable, Nylon Braided Lightning Cable 6ft", "Volt", 9, "Lightning Cables", "MFi certified."),
    ("B000000016", "Spark iPhone Charger 3 Pack, Lightning to USB Charging Cable", "Spark", 11, "Lightning Cables", "Three cords."),
    ("B000000017", "Volt USB Wall Charger Block", "Volt", 12, "Lightning Cables", "A charger in the cable leaf."),
    ("B000000018", "Loop Watch Band for Smartwatch 22mm", "Loop", 20, "Smartwatches", "A strap in the watch leaf."),
]


class FakeIndex:
    def __init__(self, chunks=(), error=None):
        self.chunks, self.error, self.calls = list(chunks), error, []

    def search(self, query, top_k, filters=()):
        self.calls.append((query, list(filters)))
        if self.error:
            raise self.error
        return self.chunks


class FakeReranker:
    def __init__(self, order):
        self.order = order

    def rerank(self, query, chunks, top_k=None):
        return [SimpleNamespace(chunk=c, rerank_score=1.0 - self.order.index(c.chunk_id) / 10) for c in chunks if c.chunk_id in self.order]


def _chunk(product, chunk_type="marketing_description", distance=0.2):
    return RetrievedChunk(f"amz_{product}:{chunk_type}", "text", {"product_id": f"amz_{product}", "chunk_type": chunk_type}, distance)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("catalog")
    source = root / "source"
    data = source / "electronics" / "data"
    data.mkdir(parents=True)
    manifest = {}
    for asin, title, brand, usd, cell, text in SAMPLES:
        pid = f"amz_{asin}"
        manifest[pid] = f"https://example.com/{asin}.jpg"
        (data / f"{pid}.json").write_text(json.dumps({
            "product_id": pid, "title": title, "brand": brand, "category": "数码电子",
            "base_price": round(usd * 7.2, 2), "image_path": "",
            "source": {"dataset": "amazon_reviews_2023", "parent_asin": asin, "image_url": manifest[pid],
                       "cell": CELLS[cell], "amazon_category": "Electronics",
                       "price_usd": usd, "average_rating": 4.5, "rating_number": 30},
            "rag_knowledge": {"marketing_description": text, "user_reviews": []},
            "skus": [],
        }), encoding="utf-8")
    (source / "image_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "sample_report.json").write_text(json.dumps({"summary": {"products": len(SAMPLES)}, "usd_cny": 7.2}))
    db = root / "catalog.sqlite3"
    build_amazon_catalog(source, db)
    with sqlite3.connect(db) as conn:
        for product, attribute in (("B000000001", "contains"), ("B000000002", "free_of")):
            conn.execute(
                "INSERT INTO product_attributes VALUES (?, ?, 'alcohol', 'observed', 'description', 0, 1, 'rule')",
                (f"amz_{product}", attribute),
            )
        for product, hours in (("B000000001", "10.0"), ("B000000002", "48.0")):  # B000000003 states none.
            conn.execute(
                "INSERT INTO product_attributes VALUES (?, 'battery_hours', ?, 'observed', 'description', 0, 1, 'rule')",
                (f"amz_{product}", hours),
            )
        conn.execute(
            "INSERT INTO product_attributes VALUES ('amz_B000000003', 'feature', 'waterproof', 'observed', 'description', 0, 1, 'rule')"
        )
        # Catalogs built before attributes-v3 stored the device a listing says it does not fit.
        conn.execute(
            "INSERT OR IGNORE INTO product_attributes VALUES ('amz_B000000012', 'compatible_device', 'iphone_15', 'observed', 'description', 0, 1, 'rule')"
        )
        reviews = (
            ("B000000006", 1, 1, "Shipping took three weeks and the package was crushed on arrival."),
            ("B000000006", 2, 2, "The slim case cracked at the corner after one week of normal use."),
            ("B000000006", 3, 5, "Great case, fits perfectly and feels solid."),
        )
        for product, index, rating, content in reviews:
            conn.execute(
                "INSERT INTO product_reviews (product_id, source_index, rating, content, polarity) VALUES (?, ?, ?, ?, ?)",
                (f"amz_{product}", index, rating, content, "negative" if rating <= 2 else "positive"),
            )
    return ProductStore(db)


@pytest.fixture(autouse=True)
def rules_only(monkeypatch):
    monkeypatch.setenv("CARTPILOT_GRAPH", "0")
    with patch.object(catalog_understanding, "_call_model", side_effect=TimeoutError("offline")):
        yield


def _ids(result):
    return [hit.product_id.removeprefix("amz_") for hit in result.hits]


def test_vector_candidates_cannot_escape_sql_filters(store):
    index = FakeIndex([_chunk("B000000003"), _chunk("B000000004"), _chunk("B000000002")])
    service = CatalogSearchService(store, index, FakeIndex(), reranker=FakeReranker([]))
    assert set(_ids(service.search("smartwatches under $50"))) == {"B000000001", "B000000002"}


def test_saved_budget_ranks_but_never_filters(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    result = service.search("smartwatches", user_profile={"budget_max": 30})
    assert result.parsed.max_price is None
    assert {"B000000001", "B000000002", "B000000003"} <= set(_ids(result))  # $42 and $65 stay visible.
    assert any(signal["kind"] == "budget" for signal in result.personalization)


def test_category_budget_applies_to_product_types_in_that_category(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    budgets = {"budget_max": 500, "category_specific": {"Electronics": {"budget_max": 30}}}
    used = [s for s in service.search("smartwatches", user_profile=budgets).personalization if s["kind"] == "budget"]
    assert [(s["scope"], s["value"]) for s in used] == [("Electronics", "-30.0")]
    other = {"category_specific": {"Beauty & Personal Care": {"budget_max": 30}}}
    assert not [s for s in service.search("smartwatches", user_profile=other).personalization if s["kind"] == "budget"]


def test_accessories_and_mislabeled_titles_stay_out_of_primary_results(store):
    service = CatalogSearchService(store, FakeIndex([_chunk("B000000004")]), FakeIndex(), reranker=FakeReranker([]))
    found = _ids(service.search("smartwatches"))
    assert "B000000004" not in found and "B000000005" not in found
    assert "B000000004" in _ids(service.search("watch bands"))


def test_rejected_attribute_uses_catalog_evidence(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    result = service.search("smartwatches without alcohol")
    checks = {hit.product_id.removeprefix("amz_"): hit.attribute_checks for hit in result.hits}
    assert "B000000001" not in checks
    assert checks["B000000002"] == {"alcohol": "free"} and checks["B000000003"] == {"alcohol": "unknown"}
    assert _ids(result)[0] == "B000000002"


def test_a_remembered_ingredient_is_marked_where_the_listing_states_it_free(store):
    # Saved memory removes listings that state it; a listing stated free of it says so.
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    result = service.search("smartwatches", user_profile={"avoid_terms": ["alcohol"]})
    checks = {hit.product_id.removeprefix("amz_"): hit.attribute_checks for hit in result.hits}
    assert "B000000001" not in checks
    assert checks["B000000002"] == {"alcohol": "free"} and not checks["B000000003"]


def test_confirmed_products_outrank_unknown_ones_beyond_the_rerank_pool(store, monkeypatch):
    monkeypatch.setenv("USE_RERANK", "1")
    monkeypatch.setattr("search.catalog_search._RERANK_POOL", 1)
    # The reranker prefers the unconfirmed watch, and the confirmed one starts outside the rerank pool.
    order = ["amz_B000000003", "amz_B000000002"]
    service = CatalogSearchService(store, FakeIndex([_chunk("B000000003")]), FakeIndex(), reranker=FakeReranker(order))
    assert _ids(service.search("smartwatches without alcohol"))[:2] == ["B000000002", "B000000003"]


def test_measured_limits_drop_stated_misses_and_rank_stated_matches_first(store, monkeypatch):
    monkeypatch.setenv("USE_RERANK", "1")
    order = ["amz_B000000003", "amz_B000000001", "amz_B000000002"]
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker(order))
    result = service.search("smartwatches with at least 20 hours of battery")
    assert result.parsed.min_price is None
    assert _ids(result) == ["B000000002", "B000000003"]  # 10 hours is stated, so that watch is out.
    assert [hit.attribute_checks for hit in result.hits] == [{"battery_hours >= 20": "meets"}, {"battery_hours >= 20": "unknown"}]


def test_review_recall_excludes_low_ratings_and_adds_evidence(store):
    reviews = FakeIndex([_chunk("B000000003", "user_review"), _chunk("B000000003", "user_review"), _chunk("B000000003", "user_review")])
    service = CatalogSearchService(store, FakeIndex(), reviews, reranker=FakeReranker([]))
    hit = next(h for h in service.search("smartwatches").hits if h.product_id.endswith("B000000003"))
    assert len([c for c in hit.evidence if c.chunk_type == "user_review"]) == 2
    assert any(c.field == "rating" and c.op == "gte" and c.value == 3 for c in reviews.calls[0][1])


def test_rerank_decides_order(store, monkeypatch):
    monkeypatch.setenv("USE_RERANK", "1")
    order = ["amz_B000000003", "amz_B000000002", "amz_B000000001"]
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker(order))
    assert _ids(service.search("smartwatches"))[:3] == ["B000000003", "B000000002", "B000000001"]


def test_graph_channel_proposes_but_sql_still_decides(store):
    class FakeGraph:
        def recall(self, query, limit):
            self.query = query
            claim = {"relation": "PRAISED_FOR", "value": "comfort", "source": "r1", "quote": "So comfy"}
            return [GraphHit("amz_B000000003", 1.0, (claim,)), GraphHit("amz_B000000004", 1.0, (claim,))]

    graph = FakeGraph()
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]), graph=graph)
    result = service.search("comfort smartwatches")
    assert graph.query.aspects == ("comfort",)
    by_id = {hit.product_id: hit for hit in result.hits}
    assert by_id["amz_B000000003"].graph_evidence[0]["quote"] == "So comfy"
    assert "amz_B000000004" not in by_id


def test_vector_failure_degrades_to_keyword_search(store):
    service = CatalogSearchService(store, FakeIndex(error=RuntimeError("down")), FakeIndex(), reranker=FakeReranker([]))
    assert "B000000001" in _ids(service.search("smartwatches"))


class RecordingReranker(FakeReranker):
    def rerank(self, query, chunks, top_k=None):
        self.documents = {c.chunk_id: c.document for c in chunks}
        return super().rerank(query, chunks, top_k)


def test_wanted_feature_from_the_listing_is_confirmed_without_hiding_silent_listings(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    result = service.search("waterproof smartwatches")
    assert result.parsed.features == ["waterproof"]
    checks = {hit.product_id.removeprefix("amz_"): hit.attribute_checks for hit in result.hits}
    assert checks["B000000003"] == {"waterproof": "meets"}
    assert checks["B000000001"] == {"waterproof": "unknown"}  # Silence is not a miss.
    assert _ids(result)[0] == "B000000003"


def test_accessories_for_another_model_of_the_device_are_left_out(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    result = service.search("iphone 15 phone case")
    found = _ids(result)
    assert "B000000007" not in found  # Names only the iPhone 13 and 14.
    assert "B000000010" not in found  # The Pro Max is another size.
    assert "B000000011" not in found  # For another phone entirely.
    assert "B000000012" not in found  # Says it does not fit the iPhone 15.
    assert "B000000013" not in found  # The title is for the Pro Max; a loose description does not change that.
    assert "B000000014" not in found  # Denied later in a list of models.
    assert {"B000000006", "B000000008", "B000000009"} <= set(found)  # Exact, universal, and "14/15".
    checks = {hit.product_id.removeprefix("amz_"): hit.attribute_checks for hit in result.hits}
    assert checks["B000000006"] == {"works with iphone 15": "meets"}
    assert checks["B000000008"] == {"works with iphone 15": "unknown"}
    assert found[0] == "B000000006"


def test_model_numbers_count_in_keyword_search(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    tokens = __import__("search.lexical", fromlist=["tokenize"]).tokenize("iPhone 15 case", model_numbers=True)
    assert "iphone15" in tokens
    ranked = service._lexical_ranking(["iphone", "iphone15", "case"], {"amz_B000000006", "amz_B000000007"}, result_type())
    assert ranked[0] == "amz_B000000006"


def result_type():
    from search.query_understanding import ParsedQuery
    return ParsedQuery(original_query="q", sub_category="Phone Basic Cases")


def test_relevance_leads_and_confirmation_only_nudges(store, monkeypatch):
    monkeypatch.setenv("USE_RERANK", "1")

    class Scores:
        def rerank(self, query, chunks, top_k=None):
            scores = {"amz_B000000001": 0.9, "amz_B000000003": 0.3, "amz_B000000002": 0.85}
            return [SimpleNamespace(chunk=c, rerank_score=scores[c.chunk_id]) for c in chunks if c.chunk_id in scores]

    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=Scores())
    # B3 confirms waterproof but the reranker finds it far less relevant: it must not jump to first.
    assert _ids(service.search("waterproof smartwatches"))[:3] == ["B000000001", "B000000002", "B000000003"]


def test_reviews_that_matched_the_request_reach_the_reranker(store, monkeypatch):
    monkeypatch.setenv("USE_RERANK", "1")
    review = RetrievedChunk("amz_B000000003:review:1", "Apex Smartwatch\nReview: Stays put on long runs.",
                            {"product_id": "amz_B000000003", "chunk_type": "user_review", "rating": 5}, 0.2)
    reranker = RecordingReranker(["amz_B000000003"])
    service = CatalogSearchService(store, FakeIndex(), FakeIndex([review]), reranker=reranker)
    service.search("smartwatches for running")
    assert "Customer reviews: Stays put on long runs." in reranker.documents["amz_B000000003"]
    assert "Customer reviews" not in reranker.documents["amz_B000000001"]


def test_cards_get_a_product_complaint_not_a_shipping_one(store, monkeypatch):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    hit = next(h for h in service.search("slim iphone 15 phone case").hits if h.product_id == "amz_B000000006")
    complaints = [c for c in hit.evidence if c.metadata.get("polarity") == "negative"]
    assert [c.document for c in complaints] == ["The slim case cracked at the corner after one week of normal use."]
    from agent.tools import evidence
    monkeypatch.setattr(evidence, "is_english", lambda: True)
    assert "cracked" in evidence.card_evidence(hit, "slim iphone 15 phone case")["risk"]


def test_a_case_for_one_variant_is_not_offered_for_another(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    found = _ids(service.search("iphone 15 pro max phone case"))
    assert set(found[:2]) == {"B000000010", "B000000013"}  # The two Pro Max cases.
    assert "B000000012" not in found  # A 15 Pro case.


def test_cables_that_list_other_models_only_rank_lower(store):
    # Only shaped accessories are filtered; a listing's model list elsewhere is rarely complete.
    from search.query_understanding import ParsedQuery
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    intent = ParsedQuery(original_query="q", compatible_with=["iphone_15", "iphone"])
    assert service._device_checks("amz_B000000002", intent.compatible_with) == {"works with iphone 15": "unknown"}


def test_cable_titles_that_lead_with_charger_are_still_cables(store):
    # "iPhone Charger Cable ... Lightning Cable" first reads as a charger, but names the cable too.
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    found = _ids(service.search("lightning cables"))
    assert {"B000000015", "B000000016"} <= set(found)
    assert "B000000017" not in found  # A charger filed as a cable names no cable.
    assert _ids(service.search("Volt Lightning Cables")) == ["B000000015"]


def test_naming_the_device_does_not_make_an_accessory_one(store):
    service = CatalogSearchService(store, FakeIndex(), FakeIndex(), reranker=FakeReranker([]))
    assert "B000000018" not in _ids(service.search("smartwatches"))  # "Watch Band for Smartwatch".
