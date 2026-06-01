from __future__ import annotations

"""端到端检索服务：自然语言 query → 排序后的商品命中列表。

主流程（hybrid retrieval 标准三段式）：

    用户原句
        │
        ▼  ① 意图理解
    understand_query(q) → ParsedQuery
        │
        ▼  ② 硬过滤 + 向量召回（Chroma 一次调用内完成）
    ChromaRetriever.search(retrieval_query, where=build_chroma_where(parsed))
        → List[RetrievedChunk]（按余弦距离排好序）
        │
        ▼  ③ 后置过滤 + 商品级聚合
    剔除含 negative_ingredients 的 chunk
    同 product_id 的多 chunk 合并，保留距离最小的一条作为代表
        │
        ▼
    List[ProductHit]（按距离升序，可继续接业务重排）

设计原则：
- 单例化：FastAPI 启动时 build 一次，避免每个请求重建 Chroma client / embedding 模型。
- 透明：每个 ProductHit 保留命中证据 chunk，方便上层 LLM 做"为什么推荐"的解释。
- 不做的事：业务重排（销量 / 评分 / 个性化）放在更上层的 Recommender，本模块只负责检索。
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from rag.retriever import ChromaRetriever, RetrievedChunk
from rag.reranker import ApiReranker, RerankedChunk, get_reranker
from search.query_understanding import ParsedQuery, understand_query
from search.where_builder import build_chroma_where

def _env_use_rerank() -> bool:
    """从环境变量 USE_RERANK 读取是否启用 API 精排，默认启用。

    设为 0/false/no/off 可禁用 reranker，链路只走向量距离排序。Docker
    也默认启用云端 rerank；需要减少 API 请求时可显式设为 0。
    """
    raw = os.getenv("USE_RERANK")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class ProductHit:
    """单个商品级别的检索命中，聚合了该商品下命中的所有 chunk。"""

    product_id: str
    title: str
    brand: str
    category: str
    sub_category: str
    base_price: float
    # 商品排序分数：启用 API rerank 后用 rerank_score（高 = 好），
    # 未启用 rerank 时退化为 -best_distance（距离越小分越高）。
    score: float
    best_distance: float | None         # 保留原始 Chroma 距离供排查
    rerank_score: float | None          # API rerank 原始分数，未 rerank 时为 None
    evidence: list[RetrievedChunk] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "sub_category": self.sub_category,
            "base_price": self.base_price,
            "score": self.score,
            "best_distance": self.best_distance,
            "rerank_score": self.rerank_score,
            "evidence_count": len(self.evidence),
            "evidence_chunk_types": [c.chunk_type for c in self.evidence],
        }


@dataclass(frozen=True)
class SearchResult:
    """一次完整检索的返回对象。"""

    parsed: ParsedQuery
    hits: list[ProductHit]
    raw_chunk_count: int                 # Chroma 原始返回的 chunk 数（去重前）
    filtered_chunk_count: int            # 经 negative_ingredients 后置过滤剩余的 chunk 数

    def to_dict(self) -> dict[str, Any]:
        return {
            "parsed": self.parsed.to_dict(),
            "raw_chunk_count": self.raw_chunk_count,
            "filtered_chunk_count": self.filtered_chunk_count,
            "hits": [hit.to_dict() for hit in self.hits],
        }


class SearchService:
    """检索服务：组合 query 理解、Chroma 召回、API 精排、后置过滤、商品聚合。"""

    def __init__(
        self,
        retriever: ChromaRetriever | None = None,
        reranker: ApiReranker | None = None,
        use_rerank: bool | None = None,
    ) -> None:
        # 允许测试时注入 mock retriever/reranker；生产环境用默认单例。
        self._retriever = retriever or ChromaRetriever()
        # use_rerank 未显式传入时由环境变量 USE_RERANK 决定（默认启用）。
        self._use_rerank = _env_use_rerank() if use_rerank is None else use_rerank
        # reranker 延迟加载：__init__ 只记录意图，首次 search 时才创建 HTTP client。
        self._reranker = reranker

    def _get_reranker(self) -> ApiReranker:
        if self._reranker is None:
            self._reranker = get_reranker()
        return self._reranker

    def search(
        self,
        user_query: str,
        top_k_chunks: int = 50,
        top_k_products: int = 10,
        base: ParsedQuery | None = None,
    ) -> SearchResult:
        """端到端入口。

        top_k_chunks: Chroma 召回的 chunk 数量。rerank 时要多召（默认 50）
                      给 API 精排选择余地；不 rerank 时 30 也够。
        top_k_products: 聚合后返回的商品数量。
        base: 上一轮结构化检索意图。非空表示这是一次"细化"——把本轮解析叠加到
              base 上（见 ParsedQuery.merge_base），避免丢失品类/价格等上下文。
        """
        # ① 意图理解（带缓存）
        parsed = understand_query(user_query)

        # ①.5 多轮细化：在上一轮意图之上叠加本轮约束。
        # 放在 needs_clarification 判断之前——有 base 时即便本轮短到被判为
        # "需澄清"，也已经有足够上下文，merge_base 会把 needs_clarification 置 False。
        if base is not None:
            parsed = parsed.merge_base(base)

        if parsed.needs_clarification:
            return SearchResult(parsed=parsed, hits=[], raw_chunk_count=0, filtered_chunk_count=0)

        # ② 硬过滤 + 向量召回
        where = build_chroma_where(parsed)
        chunks = self._retriever.search(
            query=parsed.retrieval_query or user_query,
            top_k=top_k_chunks,
            where=where,
        )
        raw_count = len(chunks)

        # ③ 后置过滤：剔除含否定成分的 chunk（在 rerank 之前做，节省 rerank 算力）
        if parsed.negative_ingredients:
            chunks = [
                c for c in chunks
                if not _contains_any(c.document, parsed.negative_ingredients)
            ]
        filtered_count = len(chunks)

        # ④ API 精排。调试智谱接入阶段不做失败降级：配置缺失、网络超时或
        # 响应格式异常都直接抛出，方便尽早发现问题。
        if self._use_rerank and chunks:
            reranked = self._get_reranker().rerank(
                query=parsed.retrieval_query or user_query,
                chunks=chunks,
            )
            hits = _aggregate_reranked_by_product(reranked)[:top_k_products]
        else:
            hits = _aggregate_by_distance(chunks)[:top_k_products]

        return SearchResult(
            parsed=parsed,
            hits=hits,
            raw_chunk_count=raw_count,
            filtered_chunk_count=filtered_count,
        )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle and needle in text for needle in needles)


def _aggregate_by_distance(chunks: list[RetrievedChunk]) -> list[ProductHit]:
    """未启用 rerank 时的聚合：按 Chroma 距离 max pooling。

    保持 chunk 进入顺序（Chroma 已按距离升序），用 dict 天然去重。
    商品分数 = -最小距离，与 rerank 路径保持“越高越好”的语义一致。
    """
    grouped: dict[str, list[RetrievedChunk]] = {}
    for chunk in chunks:
        pid = chunk.product_id
        if not pid:
            continue
        grouped.setdefault(pid, []).append(chunk)

    hits: list[ProductHit] = []
    for pid, items in grouped.items():
        leader = items[0]                # 距离最小的（Chroma 已排序，第一个即最佳）
        meta = leader.metadata
        best_distance = float(leader.distance) if leader.distance is not None else float("inf")
        hits.append(
            ProductHit(
                product_id=pid,
                title=str(meta.get("title", "")),
                brand=str(meta.get("brand", "")),
                category=str(meta.get("category", "")),
                sub_category=str(meta.get("sub_category", "")),
                base_price=float(meta.get("base_price", 0) or 0),
                score=-best_distance,
                best_distance=best_distance,
                rerank_score=None,
                evidence=items,
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def _aggregate_reranked_by_product(reranked: list[RerankedChunk]) -> list[ProductHit]:
    """启用 rerank 时的聚合：max pooling rerank_score。

    商品分数 = 该商品下最高的 rerank_score。reranked 进来时已按分数降序，
    所以第一个出现的 chunk 就是该商品下的最佳证据。

    为什么选 max pooling 而不是 mean：
    - 商品相关性取决于“最能证明它符合需求的那条证据”，而不是证据数量
    - sum/count 会让 chunk 多的商品占优势（评论多的不代表更匹配）
    - max 的潜在风险：营销文案堆关键词骱子 → 如果后续发现「尖峰赢家」质量不稳，
      可升级为 0.7*max + 0.3*mean(top3) 加权策略
    """
    grouped: dict[str, list[RerankedChunk]] = {}
    for r in reranked:
        pid = r.product_id
        if not pid:
            continue
        grouped.setdefault(pid, []).append(r)

    hits: list[ProductHit] = []
    for pid, items in grouped.items():
        leader = items[0]                # 已按 rerank_score 降序，第一个即最佳
        meta = leader.chunk.metadata
        hits.append(
            ProductHit(
                product_id=pid,
                title=str(meta.get("title", "")),
                brand=str(meta.get("brand", "")),
                category=str(meta.get("category", "")),
                sub_category=str(meta.get("sub_category", "")),
                base_price=float(meta.get("base_price", 0) or 0),
                score=leader.rerank_score,
                best_distance=float(leader.chunk.distance) if leader.chunk.distance is not None else None,
                rerank_score=leader.rerank_score,
                evidence=[r.chunk for r in items],
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


# ---------------------------------------------------------------------------
# 单例 + CLI
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_search_service() -> SearchService:
    """进程级单例。FastAPI startup 应直接调一次预热。"""
    return SearchService()


def main() -> None:
    import argparse
    import json
    import logging
    import os

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))

    parser = argparse.ArgumentParser(description="端到端检索 demo")
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--no-rerank", action="store_true", help="不走 API 精排（快但质量低）。")
    args = parser.parse_args()

    service = SearchService(use_rerank=not args.no_rerank)
    result = service.search(args.query, top_k_products=args.top_k)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
