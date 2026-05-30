from __future__ import annotations

"""API reranker.

精排原本用本地 ``sentence-transformers`` CrossEncoder，会在 Docker 镜像里拉
torch / CUDA 等超大依赖。这里改为云端 rerank API：后端只保留 HTTP 客户端，
镜像更轻，云部署也不需要本地模型缓存。
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

from rag.retriever import RetrievedChunk


DEFAULT_RERANK_PATH = "/rerank"


@dataclass(frozen=True)
class RerankedChunk:
    """带 rerank 分数的 chunk，保留原 chunk 全部信息。"""

    chunk: RetrievedChunk
    rerank_score: float

    @property
    def product_id(self) -> str:
        return self.chunk.product_id

    @property
    def chunk_type(self) -> str:
        return self.chunk.chunk_type


class ApiReranker:
    """调用兼容 ``model/query/documents/top_n`` 契约的 rerank API。"""

    def __init__(
        self,
        client: Any | None = None,
        model: str | None = None,
        path: str | None = None,
    ) -> None:
        if client is None:
            from llm.client import get_rerank_client
            client = get_rerank_client()
        if model is None:
            from llm.client import get_rerank_model_id
            model = get_rerank_model_id()
        self._client = client
        self._model = model
        self._path = (
            path
            or os.getenv("ARK_RERANKING_PATH")
            or os.getenv("ARK_RERANK_PATH")
            or DEFAULT_RERANK_PATH
        )

    def rerank(
        self,
        query: str,
        chunks: Iterable[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[RerankedChunk]:
        chunk_list = list(chunks)
        if not chunk_list:
            return []

        documents = [chunk.document for chunk in chunk_list]
        response = self._client.post(
            self._path,
            body={
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": top_k or len(documents),
            },
            cast_to=object,
        )

        scored = _parse_rerank_response(response, chunk_list)
        scored.sort(key=lambda item: item.rerank_score, reverse=True)
        if top_k is not None:
            scored = scored[:top_k]
        return scored


def _parse_rerank_response(response: Any, chunks: list[RetrievedChunk]) -> list[RerankedChunk]:
    """兼容常见 rerank API 返回格式。

    支持：
    - {"results": [{"index": 1, "relevance_score": 0.9}]}
    - {"data": [{"document_index": 1, "score": 0.9}]}
    - {"scores": [0.1, 0.9, ...]}
    """
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected rerank response type: {type(response)!r}")

    if isinstance(response.get("scores"), list):
        scores = response["scores"]
        return [
            RerankedChunk(chunk=chunk, rerank_score=float(scores[idx]))
            for idx, chunk in enumerate(chunks)
            if idx < len(scores)
        ]

    results = response.get("results") or response.get("data")
    if not isinstance(results, list):
        raise ValueError("Rerank response missing results/data/scores")

    reranked: list[RerankedChunk] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index", item.get("document_index"))
        if index is None and isinstance(item.get("document"), dict):
            index = item["document"].get("index")
        if index is None:
            continue
        idx = int(index)
        if not 0 <= idx < len(chunks):
            continue
        score = item.get("relevance_score", item.get("score", item.get("rerank_score", 0)))
        reranked.append(RerankedChunk(chunk=chunks[idx], rerank_score=float(score)))

    if not reranked:
        raise ValueError("Rerank response contains no usable scores")
    return reranked


@lru_cache(maxsize=1)
def get_reranker() -> ApiReranker:
    """进程级单例，复用 HTTP 连接池。"""
    return ApiReranker()
