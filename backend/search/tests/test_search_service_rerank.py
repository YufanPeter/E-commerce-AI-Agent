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


def _brand_chunk(pid: str, brand: str, distance: float = 0.2) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{pid}-chunk",
        document=f"{brand} document",
        metadata={
            "product_id": pid,
            "title": f"{brand} 商品",
            "brand": brand,
            "category": "数码电子",
            "sub_category": "智能手机",
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


# --------------------- Product-level negative filtering ---------------------

def _drink_chunk(pid: str, chunk_type: str, document: str, title: str, sub_category: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{pid}-{chunk_type}",
        document=document,
        metadata={
            "product_id": pid,
            "title": title,
            "brand": "品牌",
            "category": "食品生活",
            "sub_category": sub_category,
            "base_price": 5,
        },
        distance=0.3,
    )


class _MultiChunkRetriever:
    """Simulate a product whose category appears in only some of its chunks."""

    def __init__(self, chunks):
        self._chunks = chunks

    def search(self, query: str, top_k: int = 10, where=None):
        return list(self._chunks)


def test_negative_category_excludes_whole_product(monkeypatch):
    """Exclude the whole product even when its review chunk lacks the prohibited category term."""
    chunks = [
        # The review chunk lacks the category term, but title and subcategory contain it.
        _drink_chunk("dp", "user_review", "熬夜喝很提神，口感不错", "东鹏特饮 维生素功能饮料 500ml", "功能饮料"),
        _drink_chunk("dp", "marketing", "添加牛磺酸和咖啡因", "东鹏特饮 维生素功能饮料 500ml", "功能饮料"),
        # A regular beverage should remain.
        _drink_chunk("milk", "marketing", "纯牛奶醇香", "某某纯牛奶 250ml", "牛奶"),
    ]
    parsed = ParsedQuery(
        original_query="推荐饮料 不要功能饮料",
        sub_category=None,
        retrieval_query="饮料",
        negative_ingredients=["功能饮料"],
    )
    monkeypatch.setattr("search.search_service.understand_query", lambda query: parsed)

    result = SearchService(
        retriever=_MultiChunkRetriever(chunks),
        reranker=None,
        use_rerank=False,
    ).search("推荐饮料 不要功能饮料", top_k_products=5)

    pids = [hit.product_id for hit in result.hits]
    assert "dp" not in pids          # Exclude the whole product, including its review chunk.
    assert "milk" in pids            # Keep the regular beverage.


def test_negative_keyword_in_document_excludes_product(monkeypatch):
    """Exclude all product chunks when a prohibited term appears in any one chunk."""
    chunks = [
        _drink_chunk("x", "marketing", "经典原味", "X 饮料", "饮料"),
        _drink_chunk("x", "ingredient", "配料含阿斯巴甜", "X 饮料", "饮料"),
        _drink_chunk("y", "marketing", "天然无添加", "Y 饮料", "饮料"),
    ]
    parsed = ParsedQuery(
        original_query="饮料 不要阿斯巴甜",
        sub_category=None,
        retrieval_query="饮料",
        negative_ingredients=["阿斯巴甜"],
    )
    monkeypatch.setattr("search.search_service.understand_query", lambda query: parsed)

    result = SearchService(
        retriever=_MultiChunkRetriever(chunks),
        reranker=None,
        use_rerank=False,
    ).search("饮料 不要阿斯巴甜", top_k_products=5)

    pids = [hit.product_id for hit in result.hits]
    assert "x" not in pids
    assert "y" in pids


def test_sub_category_exclude_postfilter_fallback(monkeypatch):
    """Apply a post-filter fallback that excludes products matching `sub_category_exclude`.

    Simulates candidates leaking through `$nin` or a SQLite fallback to verify the final guard.
    """
    chunks = [
        _drink_chunk("dp", "user_review", "熬夜喝很提神", "东鹏特饮 500ml", "功能饮料"),
        _drink_chunk("dp", "marketing", "牛磺酸咖啡因", "东鹏特饮 500ml", "功能饮料"),
        _drink_chunk("rb", "marketing", "红牛维生素", "红牛 250ml", "功能饮料"),
        _drink_chunk("tea", "marketing", "无糖乌龙茶", "东方树叶 500ml", "茶饮"),
    ]
    parsed = ParsedQuery(
        original_query="推荐饮料 不要功能饮料",
        retrieval_query="饮料",
        sub_category_exclude=["功能饮料"],
    )
    monkeypatch.setattr("search.search_service.understand_query", lambda query: parsed)

    result = SearchService(
        retriever=_MultiChunkRetriever(chunks),
        reranker=None,
        use_rerank=False,
    ).search("推荐饮料 不要功能饮料", top_k_products=5)

    pids = [hit.product_id for hit in result.hits]
    assert "dp" not in pids   # Exclude the first energy drink.
    assert "rb" not in pids   # Exclude the second energy drink.
    assert "tea" in pids      # Keep the tea.


def test_synonym_negative_normalized_then_excludes(monkeypatch):
    """Normalize a user or LLM synonym before excluding the matching catalog category."""
    chunks = [
        _drink_chunk("rb", "user_review", "提神效果好", "红牛 250ml", "功能饮料"),
        _drink_chunk("tea", "marketing", "清爽乌龙", "东方树叶 500ml", "茶饮"),
    ]
    parsed = ParsedQuery(
        original_query="饮料 不要能量饮料",
        retrieval_query="饮料",
        negative_ingredients=["能量饮料"],  # Deliberately use a synonym to exercise normalization.
    )
    monkeypatch.setattr("search.search_service.understand_query", lambda query: parsed)

    result = SearchService(
        retriever=_MultiChunkRetriever(chunks),
        reranker=None,
        use_rerank=False,
    ).search("饮料 不要能量饮料", top_k_products=5)

    pids = [hit.product_id for hit in result.hits]
    assert "rb" not in pids
    assert "tea" in pids


def test_brand_exclude_postfilter_blocks_leaked_candidates(monkeypatch):
    """Enforce brand exclusions even if the retriever ignores `where` and leaks candidates."""
    chunks = [
        _brand_chunk("iphone", "Apple 苹果", 0.1),
        _brand_chunk("huawei", "华为", 0.2),
        _brand_chunk("vivo", "vivo", 0.3),
    ]
    parsed = ParsedQuery(
        original_query="非华为非苹果的手机",
        retrieval_query="手机",
        brand_exclude=["华为", "苹果"],
    )
    monkeypatch.setattr("search.search_service.understand_query", lambda query: parsed)

    result = SearchService(
        retriever=_MultiChunkRetriever(chunks),
        reranker=None,
        use_rerank=False,
    ).search("非华为非苹果的手机", top_k_products=5)

    brands = [hit.brand for hit in result.hits]
    assert brands == ["vivo"]
