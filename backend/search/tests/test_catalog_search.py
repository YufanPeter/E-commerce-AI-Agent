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
}
SAMPLES = [
    ("B000000001", "Atlas Smartwatch", "Atlas", 25, "Smartwatches", "A rugged watch for hiking."),
    ("B000000002", "Summit Smartwatch", "Summit", 42, "Smartwatches", "A slim watch for the office."),
    ("B000000003", "Apex Smartwatch", "Apex", 65, "Smartwatches", "A bright watch for runners."),
    ("B000000004", "Atlas Replacement Band", "Atlas", 8, "Smartwatch Bands", "A soft strap."),
    ("B000000005", "Vector Smart Watch Bands 20mm", "Vector", 30, "Smartwatches", "Straps in a mislabeled leaf."),
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
