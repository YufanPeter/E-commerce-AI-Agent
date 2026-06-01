from __future__ import annotations

import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rag.reranker import ApiReranker, _parse_rerank_scores
from rag.retriever import RetrievedChunk


def _chunk(pid: str, document: str, distance: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{pid}-chunk",
        document=document,
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


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """模拟智谱 HTTP rerank：记录请求、按构造时给定的 payload 返回。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, *, json):
        self.calls.append({"url": url, "json": json})
        return _FakeResponse(self.payload)


def test_api_reranker_sends_query_and_documents_and_sorts_by_score():
    chunks = [_chunk("a", "轻量跑鞋", 0.4), _chunk("b", "厚底跑鞋", 0.2)]
    client = _FakeClient(payload={"scores": [0.2, 0.9]})

    ranked = ApiReranker(
        client=client,
        model="rerank",
        base_url="https://open.bigmodel.cn/api/paas/v4/rerank",
    ).rerank("轻量跑鞋", chunks)

    # 分数与输入同序对齐后按降序排序：b(0.9) 在前。
    assert [item.product_id for item in ranked] == ["b", "a"]
    call = client.calls[0]
    assert call["url"] == "https://open.bigmodel.cn/api/paas/v4/rerank"
    assert call["json"] == {
        "model": "rerank",
        "query": "轻量跑鞋",
        "documents": ["轻量跑鞋", "厚底跑鞋"],
        "top_n": 2,
    }


def test_api_reranker_respects_top_k():
    chunks = [_chunk("a", "A", 0.4), _chunk("b", "B", 0.2), _chunk("c", "C", 0.1)]
    client = _FakeClient(payload={"scores": [0.1, 0.9, 0.5]})

    ranked = ApiReranker(client=client, model="rerank", base_url="u").rerank("q", chunks, top_k=2)

    assert [item.product_id for item in ranked] == ["b", "c"]


def test_parse_rerank_scores_returns_aligned_floats():
    assert _parse_rerank_scores({"scores": [0.1, 0.8]}, ["A", "B"]) == [0.1, 0.8]


def test_parse_rerank_scores_aligns_zhipu_results_by_document():
    response = {
        "results": [
            {"document": "B", "relevance_score": 0.8},
            {"document": "A", "relevance_score": 0.2},
        ]
    }

    assert _parse_rerank_scores(response, ["A", "B"]) == [0.2, 0.8]


def test_parse_rerank_scores_rejects_empty():
    with pytest.raises(ValueError):
        _parse_rerank_scores({"results": []}, ["A", "B"])


def test_reranker_module_does_not_import_sentence_transformers():
    assert "sentence_transformers" not in sys.modules
    assert "torch" not in sys.modules
