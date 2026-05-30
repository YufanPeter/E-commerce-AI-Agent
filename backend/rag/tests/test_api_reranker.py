from __future__ import annotations

import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rag.reranker import ApiReranker, _parse_rerank_response
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


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, path, body, cast_to):
        self.calls.append({"path": path, "body": body, "cast_to": cast_to})
        return self.response


def test_api_reranker_posts_query_and_documents():
    chunks = [_chunk("a", "轻量跑鞋", 0.4), _chunk("b", "厚底跑鞋", 0.2)]
    client = _FakeClient({"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]})

    ranked = ApiReranker(client=client, model="doubao-seed-rerank", path="/rerank").rerank("轻量跑鞋", chunks)

    assert [item.product_id for item in ranked] == ["b", "a"]
    assert client.calls[0]["path"] == "/rerank"
    assert client.calls[0]["body"]["model"] == "doubao-seed-rerank"
    assert client.calls[0]["body"]["query"] == "轻量跑鞋"
    assert client.calls[0]["body"]["documents"] == ["轻量跑鞋", "厚底跑鞋"]


def test_parse_rerank_response_supports_scores_array():
    chunks = [_chunk("a", "A", 0.4), _chunk("b", "B", 0.2)]
    ranked = _parse_rerank_response({"scores": [0.1, 0.8]}, chunks)
    assert [(item.product_id, item.rerank_score) for item in ranked] == [("a", 0.1), ("b", 0.8)]


def test_reranker_module_does_not_import_sentence_transformers():
    assert "sentence_transformers" not in sys.modules
    assert "torch" not in sys.modules
