"""SKU 价格区间：where 子句口径、召回命中的区间字段、建库元数据。"""

from __future__ import annotations

from rag.build_chroma import price_range_metadata
from rag.retriever import RetrievedChunk
from search.query_understanding import ParsedQuery
from search.search_service import _aggregate_by_distance
from search.where_builder import build_chroma_where


def test_where_uses_sku_range_not_base_price():
    where = build_chroma_where(ParsedQuery(original_query="q", max_price=500))
    assert where == {"min_price": {"$lte": 500.0}}
    where = build_chroma_where(ParsedQuery(original_query="q", min_price=200, max_price=500))
    assert where == {"$and": [{"max_price": {"$gte": 200.0}}, {"min_price": {"$lte": 500.0}}]}


def test_price_range_metadata_from_skus():
    product = {"base_price": 600, "skus": [{"price": 450}, {"price": 900}, {"price": 700, "status": "inactive"}]}
    assert price_range_metadata(product) == {"min_price": 450.0, "max_price": 900.0, "sku_count": 2}
    assert price_range_metadata({"base_price": 99, "skus": []}) == {"min_price": 99.0, "max_price": 99.0, "sku_count": 0}


def test_hit_carries_price_range_and_falls_back_to_base_price():
    with_range = RetrievedChunk(
        chunk_id="a:1", document="x", distance=0.1,
        metadata={"product_id": "a", "title": "A", "base_price": 600, "min_price": 450, "max_price": 900, "sku_count": 2},
    )
    legacy = RetrievedChunk(
        chunk_id="b:1", document="y", distance=0.2,
        metadata={"product_id": "b", "title": "B", "base_price": 300},
    )
    hits = {h.product_id: h for h in _aggregate_by_distance([with_range, legacy])}
    assert (hits["a"].min_price, hits["a"].max_price, hits["a"].sku_count) == (450.0, 900.0, 2)
    assert (hits["b"].min_price, hits["b"].max_price, hits["b"].sku_count) == (300.0, 300.0, 0)
    assert hits["a"].to_dict()["max_price"] == 900.0
