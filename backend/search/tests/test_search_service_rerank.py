from __future__ import annotations

import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from search.query_understanding import ParsedQuery
from search.search_service import SearchService
from rag.retriever import RetrievedChunk


def _chunk(pid: str, distance: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{pid}-chunk",
        document=f"{pid} document",
        metadata={
            "product_id": pid,
            "title": f"商品{pid}",
            "brand": "品牌",
            "category": "类目",
            "sub_category": "子类目",
            "base_price": 100,
        },
        distance=distance,
    )


class _FakeRetriever:
    def search(self, query: str, top_k: int = 10, where=None):
        return [_chunk("a", 0.4), _chunk("b", 0.2)]


class _FakeReranker:
    def rerank(self, query, chunks, top_k=None):
        from rag.reranker import RerankedChunk

        chunk_list = list(chunks)
        return [
            RerankedChunk(chunk=chunk_list[0], rerank_score=0.95),
            RerankedChunk(chunk=chunk_list[1], rerank_score=0.10),
        ]


class _BoomReranker:
    def rerank(self, query, chunks, top_k=None):
        raise RuntimeError("rerank api down")


def _parsed() -> ParsedQuery:
    return ParsedQuery(original_query="轻量跑鞋", sub_category="跑鞋", retrieval_query="轻量 跑鞋")


def test_search_service_uses_api_rerank(monkeypatch):
    monkeypatch.setattr("search.search_service.understand_query", lambda query: _parsed())

    result = SearchService(
        retriever=_FakeRetriever(),
        reranker=_FakeReranker(),
        use_rerank=True,
    ).search("轻量跑鞋", top_k_products=2)

    assert [hit.product_id for hit in result.hits] == ["a", "b"]
    assert result.hits[0].rerank_score == 0.95


def test_search_service_raises_when_api_rerank_fails(monkeypatch):
    monkeypatch.setattr("search.search_service.understand_query", lambda query: _parsed())

    import pytest

    with pytest.raises(RuntimeError, match="rerank api down"):
        SearchService(
            retriever=_FakeRetriever(),
            reranker=_BoomReranker(),
            use_rerank=True,
        ).search("轻量跑鞋", top_k_products=2)
