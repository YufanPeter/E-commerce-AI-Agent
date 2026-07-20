from __future__ import annotations

"""API-backed reranker.

Cloud reranking replaces a local ``sentence-transformers`` cross-encoder, keeping the
backend image free of large PyTorch/CUDA dependencies and local model caches.

The configured response may contain ranked documents and relevance scores without input
indices. Scores must therefore be aligned back to the original documents before sorting.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Iterable

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
    """Call the reranking API and align relevance scores to input documents."""

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

    def rerank(
        self,
        query: str,
        chunks: Iterable[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[RerankedChunk]:
        chunk_list = list(chunks)
        if not chunk_list:
            return []

        # Bound request size; upstream retrieval normally returns at most 50 chunks.
        limited_chunks = chunk_list[:50]
        documents = [chunk.document for chunk in limited_chunks]
        payload = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        }
        response = self._client.post(self._base_url, json=payload)
        response.raise_for_status()
        scores = _parse_rerank_scores(response.json(), documents)

        scored = [
            RerankedChunk(chunk=chunk, rerank_score=float(score))
            for chunk, score in zip(limited_chunks, scores)
        ]
        scored.sort(key=lambda item: item.rerank_score, reverse=True)
        if top_k is not None:
            scored = scored[:top_k]
        return scored


def _parse_rerank_scores(response: Any, documents: list[str]) -> list[float]:
    """Extract scores aligned to the input ``documents`` order.

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
