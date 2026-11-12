from __future__ import annotations

"""API reranker（百炼 qwen3-rerank 主用，智谱 Rerank 兜底）。

精排原本用本地 ``sentence-transformers`` CrossEncoder，会在 Docker 镜像里拉
torch / CUDA 等超大依赖。这里使用云端 rerank API，客户端层负责百炼到
智谱的自动 fallback，因此后端镜像无需本地模型缓存。

The configured response may contain ranked documents and relevance scores without input
indices. Scores must therefore be aligned back to the original documents before sorting.
"""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from typing import TYPE_CHECKING, Any, Iterable

from llm.cache import TTLCache
from rag.retriever import RetrievedChunk

if TYPE_CHECKING:
    import httpx

@dataclass(frozen=True)
class RerankedChunk:
    """Retrieved chunk paired with its reranking score."""

    chunk: RetrievedChunk
    rerank_score: float

    @property
    def product_id(self) -> str:
        return self.chunk.product_id

    @property
    def chunk_type(self) -> str:
        return self.chunk.chunk_type


class ApiReranker:
    """调用云端 rerank，返回与输入同序对齐的相关性分数。"""

    def __init__(
        self,
        client: "httpx.Client | None" = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if client is None:
            from llm.client import get_rerank_client
            client = get_rerank_client()
        if model is None:
            from llm.client import get_rerank_model_id
            model = get_rerank_model_id()
        if base_url is None:
            from llm.client import get_rerank_base_url
            base_url = get_rerank_base_url()
        self._client = client
        self._model = model
        self._base_url = base_url
        # Same query over the same documents scores the same. A repeated search hits every
        # other cache (route, parse, embedding, narrative), so this call was all that remained.
        self._scores: TTLCache[list[float]] = TTLCache(maxsize=512)

    def rerank(
        self,
        query: str,
        chunks: Iterable[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[RerankedChunk]:
        chunk_list = list(chunks)
        if not chunk_list:
            return []

        # 上游召回通常 ≤50，这里做保护性截断，也兼容 fallback 服务限制。
        limited_chunks = chunk_list[:50]
        documents = [chunk.document for chunk in limited_chunks]
        key = (self._model, query, tuple(hashlib.sha1(d.encode("utf-8")).hexdigest() for d in documents))
        scores = self._scores.get(key)
        if scores is None:
            payload = {
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            }
            response = self._client.post(self._base_url, json=payload)
            response.raise_for_status()
            scores = _parse_rerank_scores(response.json(), documents)
            self._scores.set(key, scores)

        scored = [
            RerankedChunk(chunk=chunk, rerank_score=float(score))
            for chunk, score in zip(limited_chunks, scores)
        ]
        scored.sort(key=lambda item: item.rerank_score, reverse=True)
        if top_k is not None:
            scored = scored[:top_k]
        return scored


def _parse_rerank_scores(response: Any, documents: list[str]) -> list[float]:
    """从 rerank 响应取出与输入 ``documents`` 同序对齐的分数列表。

    Supports indexed results, document-valued results, and a flat ``scores`` array.
    """
    expected = len(documents)
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected rerank response type: {type(response)!r}")

    # A flat score array is already aligned.
    flat = response.get("scores")
    if isinstance(flat, list) and flat:
        if len(flat) < expected:
            raise ValueError(f"Rerank returned {len(flat)} scores for {expected} documents")
        return [float(s) for s in flat[:expected]]

    results = response.get("results") or response.get("data")
    if isinstance(results, dict):
        results = results.get("results") or results.get("data")
    if not isinstance(results, list) or not results:
        raise ValueError(f"Rerank response missing results/scores: {response!r}")

    aligned: list[float | None] = [None] * expected
    # Duplicate documents consume available input positions in appearance order.
    doc_positions: dict[str, list[int]] = {}
    for idx, doc in enumerate(documents):
        doc_positions.setdefault(doc, []).append(idx)

    for item in results:
        if not isinstance(item, dict):
            continue
        score = item.get("relevance_score")
        if score is None:
            score = item.get("score", item.get("rerank_score"))
        if score is None:
            continue

        index = item.get("index")
        if isinstance(index, int) and 0 <= index < expected:
            aligned[index] = float(score)
            continue

        doc = item.get("document")
        if isinstance(doc, dict):  # Some providers wrap document text in an object.
            doc = doc.get("text")
        if isinstance(doc, str) and doc_positions.get(doc):
            aligned[doc_positions[doc].pop(0)] = float(score)

    missing = [i for i, s in enumerate(aligned) if s is None]
    if missing:
        raise ValueError(f"Rerank response missing scores for indexes: {missing}")
    return [float(s) for s in aligned]


@lru_cache(maxsize=1)
def get_reranker() -> ApiReranker:
    """Return a process-wide reranker that reuses its HTTP connection pool."""
    return ApiReranker()
