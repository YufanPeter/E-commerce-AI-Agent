from __future__ import annotations

"""API reranker（智谱 Rerank）。

精排原本用本地 ``sentence-transformers`` CrossEncoder，会在 Docker 镜像里拉
torch / CUDA 等超大依赖。这里改为云端 rerank API：调用智谱
``/paas/v4/rerank``，通过 ``ZHIPU_API_KEY`` 鉴权。后端镜像
因此更轻，云部署也不再需要本地模型缓存。

智谱 rerank 的响应是按相关性降序排好的 ``results``，每项带 ``document`` 文本和
``relevance_score``，但**不带原始 index**。因此分数要按 document 文本回填到输入
顺序，再由上层重新排序，而不能假设返回顺序等于输入顺序。
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Iterable

from rag.retriever import RetrievedChunk

if TYPE_CHECKING:
    import httpx

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
    """调用智谱 rerank：输入 query + documents，返回与输入同序对齐的相关性分数。"""

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

        # 智谱 rerank 单次请求不宜过大；上游召回通常 ≤50，这里做保护性截断。
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
    """从智谱 rerank 响应取出与输入 ``documents`` 同序对齐的分数列表。

    兼容三种返回形态：
    - results[].index + relevance_score（按 index 回填，最稳）
    - results[].document + relevance_score（智谱实际格式，按文本回填）
    - scores: [...]（直接同序数组）
    """
    expected = len(documents)
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected rerank response type: {type(response)!r}")

    # 形态三：直接的同序分数数组。
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
    # document 文本可能重复：用「剩余可用位置」队列按出现顺序消费。
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
        if isinstance(doc, dict):  # 个别实现把文本包在 {"text": ...} 里
            doc = doc.get("text")
        if isinstance(doc, str) and doc_positions.get(doc):
            aligned[doc_positions[doc].pop(0)] = float(score)

    missing = [i for i, s in enumerate(aligned) if s is None]
    if missing:
        raise ValueError(f"Rerank response missing scores for indexes: {missing}")
    return [float(s) for s in aligned]


@lru_cache(maxsize=1)
def get_reranker() -> ApiReranker:
    """进程级单例，复用已建立的 HTTP 连接池。"""
    return ApiReranker()
