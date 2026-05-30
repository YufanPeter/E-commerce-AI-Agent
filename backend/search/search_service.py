from __future__ import annotations

"""端到端检索服务：自然语言 query → 排序后的商品命中列表。

主流程（hybrid retrieval 标准四段式）：

    用户原句
        │
        ▼  ① 意图理解
    understand_query(q) → ParsedQuery
        │
        ▼  ② SQLite 硬匹配
    ProductStore.find_candidates(parsed.hard_filters) → hard_product_ids
        │
        ▼  ③ RAG 软匹配（只在 hard_product_ids 里做语义召回）
    ChromaRetriever.search(retrieval_query, where={"product_id": {"$in": hard_product_ids}})
        → List[RetrievedChunk]（按余弦距离排好序）
        │
        ▼  ④ 后置过滤 + 商品级聚合
    剔除含 negative_ingredients 的 chunk
    同 product_id 的多 chunk 合并，保留最佳证据，并用 SQLite 商品事实回填
        │
        ▼
    List[ProductHit]（按距离升序，可继续接业务重排）

设计原则：
- 单例化：FastAPI 启动时 build 一次，避免每个请求重建 Chroma client / embedding 模型。
- 透明：每个 ProductHit 保留命中证据 chunk，方便上层 LLM 做"为什么推荐"的解释。
- 不做的事：业务重排（销量 / 评分 / 个性化）放在更上层的 Recommender，本模块只负责检索。
"""

from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from rag.retriever import ChromaRetriever, RetrievedChunk
from rag.reranker import CrossEncoderReranker, RerankedChunk, get_reranker
from search.query_understanding import ParsedQuery, expand_brands, understand_query
from store.product_store import ProductCandidate, ProductStore


@dataclass(frozen=True)
class ProductHit:
    """单个商品级别的检索命中，聚合了该商品下命中的所有 chunk。"""

    product_id: str
    title: str
    brand: str
    category: str
    sub_category: str
    base_price: float
    # 商品排序分数：加了 rerank 后用 rerank_score（高 = 好），
    # 未启用 rerank 时退化为 -best_distance（距离越小分越高）。
    score: float
    best_distance: float | None         # 保留原始 Chroma 距离供排查
    rerank_score: float | None          # CrossEncoder 原始分数，未 rerank 时为 None
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
    hard_product_ids: list[str]           # SQLite 硬匹配通过的商品 id
    rag_product_ids: list[str]            # RAG 软召回命中的商品 id（去重后）
    matched_product_ids: list[str]        # 最终交集聚合并排序后的商品 id
    raw_chunk_count: int                 # Chroma 原始返回的 chunk 数（去重前）
    filtered_chunk_count: int            # 经 negative_ingredients 后置过滤剩余的 chunk 数
    # None = 严格匹配命中；非空 = 触发了零命中降级，文字说明放宽了哪些条件。
    fallback_reason: str | None = None
    # 机读：被放宽的维度 id 列表（见 _RELAXATION_LABELS），方便前端按维度提示。
    relaxed_filters: list[str] = field(default_factory=list)

    @property
    def is_fallback(self) -> bool:
        """True 表示返回的是放宽后的兜底结果，前端必须与严格结果分开展示。"""
        return self.fallback_reason is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parsed": self.parsed.to_dict(),
            "hard_product_ids": self.hard_product_ids,
            "rag_product_ids": self.rag_product_ids,
            "matched_product_ids": self.matched_product_ids,
            "raw_chunk_count": self.raw_chunk_count,
            "filtered_chunk_count": self.filtered_chunk_count,
            "is_fallback": self.is_fallback,
            "fallback_reason": self.fallback_reason,
            "relaxed_filters": self.relaxed_filters,
            "hits": [hit.to_dict() for hit in self.hits],
        }


@dataclass(frozen=True)
class _PipelineOutput:
    """_run_pipeline 的内部返回：一次检索流水线的命中与计数。"""

    hits: list[ProductHit]
    hard_product_ids: list[str]
    rag_product_ids: list[str]
    raw_chunk_count: int
    filtered_chunk_count: int


class SearchService:
    """检索服务：组合 query 理解、Chroma 召回、CrossEncoder 精排、后置过滤、商品聚合。"""

    def __init__(
        self,
        retriever: ChromaRetriever | None = None,
        reranker: CrossEncoderReranker | None = None,
        product_store: ProductStore | None = None,
        use_rerank: bool = True,
    ) -> None:
        # 允许测试时注入 mock retriever/reranker；生产环境用默认单例。
        self._retriever = retriever or ChromaRetriever()
        self._product_store = product_store or ProductStore()
        self._use_rerank = use_rerank
        # reranker 延迟加载：__init__ 只记录意图，首次 search 时才下模型，
        # 避免 use_rerank=False 的场景白费 ~280MB 内存 + ~2s 启动。
        self._reranker = reranker

    def _get_reranker(self) -> CrossEncoderReranker:
        if self._reranker is None:
            self._reranker = get_reranker()
        return self._reranker

    def search(
        self,
        user_query: str,
        top_k_chunks: int = 50,
        top_k_products: int = 10,
    ) -> SearchResult:
        """端到端入口。

        top_k_chunks: Chroma 召回的 chunk 数量。rerank 续班时要多召（默认 50）
                      给 CrossEncoder 选择余地；不 rerank 时 30 也够。
        top_k_products: 聚合后返回的商品数量。

        命中策略：先按用户原意做严格匹配；严格匹配为空时，按品类放宽阶梯
        逐级放宽硬条件（见 _search_with_fallback）。严格结果与 fallback 结果
        互斥返回，由 SearchResult.is_fallback / fallback_reason 区分。
        """
        # ① 意图理解（带缓存）
        parsed = understand_query(user_query)

        if parsed.needs_clarification:
            return self._empty_result(parsed)

        # ② 严格匹配：所有硬条件都按用户原意走一遍流水线。
        strict = self._run_pipeline(parsed, user_query, top_k_chunks, top_k_products)
        if strict.hits:
            return self._build_result(parsed, strict, fallback_reason=None, relaxed_filters=[])

        # ③ 零命中 → 分层 fallback（严格为空才触发，二者不会混在同一结果里）。
        return self._search_with_fallback(parsed, user_query, top_k_chunks, top_k_products)

    def _search_with_fallback(
        self,
        parsed: ParsedQuery,
        user_query: str,
        top_k_chunks: int,
        top_k_products: int,
    ) -> SearchResult:
        """零命中分层降级：按品类放宽阶梯逐级放宽，返回第一个有命中的层级。

        - 阶梯由 _relaxation_ladder(parsed) 决定，品类可定制（如护肤绝不放品牌）。
        - 累积放宽：第 k 层在前 k-1 层基础上再松一项，保证范围单调变宽。
        - 某一步没有可放宽的内容（如本来就没填价格）则跳过，不计入 fallback_reason。
        - 末级 full_vector 跳过硬匹配做全库纯向量召回，作为最终兜底。
        - 注意：negative_ingredients（如“不要含酒精”）是用户的硬约束，任何层级都不放宽。
        """
        relaxed = parsed
        applied: list[str] = []

        for step in _relaxation_ladder(parsed):
            if step == "full_vector":
                applied.append(step)
                out = self._run_pipeline(
                    relaxed, user_query, top_k_chunks, top_k_products, full_vector=True
                )
            else:
                widened = _apply_relaxation(relaxed, step)
                if widened == relaxed:
                    continue  # 这一步没有可放宽的条件，直接看下一项
                relaxed = widened
                applied.append(step)
                out = self._run_pipeline(relaxed, user_query, top_k_chunks, top_k_products)

            if out.hits:
                return self._build_result(
                    parsed,
                    out,
                    fallback_reason=_describe_relaxation(applied),
                    relaxed_filters=applied,
                )

        # 放宽到底仍零命中（极少见，例如空库或被否定成分全过滤）。
        return self._build_result(
            parsed,
            _PipelineOutput(hits=[], hard_product_ids=[], rag_product_ids=[], raw_chunk_count=0, filtered_chunk_count=0),
            fallback_reason=_describe_relaxation(applied) if applied else "无任何匹配结果",
            relaxed_filters=applied,
        )

    def _run_pipeline(
        self,
        parsed: ParsedQuery,
        user_query: str,
        top_k_chunks: int,
        top_k_products: int,
        full_vector: bool = False,
    ) -> _PipelineOutput:
        """单次检索流水线：硬匹配 → Chroma 召回 → 否定过滤 → 精排 → 商品聚合。

        full_vector=True 时跳过 SQLite 硬匹配，对全库做纯向量召回（fallback 末级）。
        """
        retrieval_query = parsed.retrieval_query or user_query

        if full_vector:
            chunks = self._retriever.search(query=retrieval_query, top_k=top_k_chunks, where=None)
            hard_product_ids: list[str] = []
            candidates_by_id: dict[str, ProductCandidate] = {}
        else:
            hard_candidates = self._hard_match_candidates(parsed)
            hard_product_ids = [c.product_id for c in hard_candidates]
            if not hard_product_ids:
                return _PipelineOutput([], [], [], 0, 0)
            chunks = self._retriever.search(
                query=retrieval_query,
                top_k=top_k_chunks,
                where=_build_product_id_where(hard_product_ids),
            )
            candidates_by_id = {c.product_id: c for c in hard_candidates}

        raw_count = len(chunks)

        # 后置过滤：剔除含否定成分的 chunk（在 rerank 之前，省算力）。任何层级都执行。
        if parsed.negative_ingredients:
            chunks = [
                c for c in chunks
                if not _contains_any(c.document, parsed.negative_ingredients)
            ]
        filtered_count = len(chunks)

        if full_vector:
            # 全库召回没有硬匹配候选，按命中的 product_id 回表补商品事实。
            candidates_by_id = {
                c.product_id: c
                for c in self._product_store.get_products_by_ids(_unique_product_ids(chunks))
            }

        if self._use_rerank and chunks:
            reranked = self._get_reranker().rerank(query=retrieval_query, chunks=chunks)
            hits = _aggregate_reranked_by_product(reranked, candidates_by_id)[:top_k_products]
        else:
            hits = _aggregate_by_distance(chunks, candidates_by_id)[:top_k_products]

        return _PipelineOutput(
            hits=hits,
            hard_product_ids=hard_product_ids,
            rag_product_ids=_unique_product_ids(chunks),
            raw_chunk_count=raw_count,
            filtered_chunk_count=filtered_count,
        )

    def _build_result(
        self,
        parsed: ParsedQuery,
        out: _PipelineOutput,
        fallback_reason: str | None,
        relaxed_filters: list[str],
    ) -> SearchResult:
        """把流水线输出包成 SearchResult。parsed 始终保留用户原始意图。"""
        return SearchResult(
            parsed=parsed,
            hits=out.hits,
            hard_product_ids=out.hard_product_ids,
            rag_product_ids=out.rag_product_ids,
            matched_product_ids=[hit.product_id for hit in out.hits],
            raw_chunk_count=out.raw_chunk_count,
            filtered_chunk_count=out.filtered_chunk_count,
            fallback_reason=fallback_reason,
            relaxed_filters=relaxed_filters,
        )

    def _empty_result(self, parsed: ParsedQuery) -> SearchResult:
        return SearchResult(
            parsed=parsed,
            hits=[],
            hard_product_ids=[],
            rag_product_ids=[],
            matched_product_ids=[],
            raw_chunk_count=0,
            filtered_chunk_count=0,
            fallback_reason=None,
            relaxed_filters=[],
        )

    def _hard_match_candidates(self, parsed: ParsedQuery) -> list[ProductCandidate]:
        """用 SQLite 做确定性硬匹配，返回后续 RAG 可搜索的候选商品。"""
        return self._product_store.find_candidates(
            category=parsed.category,
            sub_category=parsed.sub_category,
            min_price=parsed.min_price,
            max_price=parsed.max_price,
            brand_include=expand_brands(parsed.brand_include),
            brand_exclude=expand_brands(parsed.brand_exclude),
        )


# ---------------------------------------------------------------------------
# 零命中分层 fallback：放宽阶梯
# ---------------------------------------------------------------------------

# 默认放宽顺序：先松价格 → 松品牌排除 → 松指定品牌 → 子类目上推到大类 →
# 末级全库纯向量召回。每一层都比上一层更宽，保证单调放宽。
DEFAULT_RELAXATION_LADDER: tuple[str, ...] = (
    "price",
    "brand_exclude",
    "brand_include",
    "sub_category",
    "full_vector",
)

# 品类级覆盖：不同品类的放宽顺序不同。这是 LLM relaxation_priority 的静态兜底版，
# 未来可由 LLM 按 query 输出顺序注入覆盖本表。
#   - 美妆护肤：绝不放品牌（删掉 brand_* 两步），且不做 full_vector
#     （全库召回会带出其它品牌，等于变相放品牌）；品牌即信任与功效背书，
#     宁可返回空也不串品牌。
#   - 食品饮料：子类目可早放（口味/品类替代性强）。
CATEGORY_RELAXATION_LADDER: dict[str, tuple[str, ...]] = {
    "美妆护肤": ("price", "sub_category"),
    "食品饮料": ("sub_category", "price", "brand_exclude", "brand_include", "full_vector"),
}

_RELAXATION_LABELS: dict[str, str] = {
    "price": "价格区间",
    "brand_exclude": "品牌排除",
    "brand_include": "指定品牌",
    "sub_category": "细分类目（上推到大类）",
    "full_vector": "全部硬性条件（全库语义检索）",
}


def _relaxation_ladder(parsed: ParsedQuery) -> tuple[str, ...]:
    """返回该 query 的放宽阶梯；品类有定制则用定制顺序，否则用默认阶梯。"""
    if parsed.category and parsed.category in CATEGORY_RELAXATION_LADDER:
        return CATEGORY_RELAXATION_LADDER[parsed.category]
    return DEFAULT_RELAXATION_LADDER


def _apply_relaxation(parsed: ParsedQuery, step: str) -> ParsedQuery:
    """在 parsed 基础上放宽指定维度，返回新的 ParsedQuery。

    无可放宽时（如该维度本就为空）返回等价对象，由调用方用 == 判断后跳过。
    """
    if step == "price":
        return replace(parsed, min_price=None, max_price=None)
    if step == "brand_exclude":
        return replace(parsed, brand_exclude=[])
    if step == "brand_include":
        return replace(parsed, brand_include=[])
    if step == "sub_category":
        return replace(parsed, sub_category=None)
    return parsed


def _describe_relaxation(applied: list[str]) -> str:
    """把放宽过的维度拼成给前端/下游 LLM 看的中文说明。"""
    labels = [_RELAXATION_LABELS.get(step, step) for step in applied]
    if not labels:
        return "未放宽任何条件"
    return "未找到完全匹配的商品，已放宽：" + "、".join(labels)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle and needle in text for needle in needles)


def _build_product_id_where(product_ids: list[str]) -> dict[str, Any]:
    """把 SQLite hard ids 转成 Chroma metadata where。"""
    if len(product_ids) == 1:
        return {"product_id": product_ids[0]}
    return {"product_id": {"$in": product_ids}}


def _unique_product_ids(chunks: list[RetrievedChunk]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for chunk in chunks:
        product_id = chunk.product_id
        if product_id and product_id not in seen:
            seen.add(product_id)
            out.append(product_id)
    return out


def _aggregate_by_distance(
    chunks: list[RetrievedChunk],
    candidates_by_id: dict[str, ProductCandidate],
) -> list[ProductHit]:
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
        candidate = candidates_by_id.get(pid)
        if candidate is None:
            continue
        leader = items[0]                # 距离最小的（Chroma 已排序，第一个即最佳）
        best_distance = float(leader.distance) if leader.distance is not None else float("inf")
        hits.append(
            ProductHit(
                product_id=pid,
                title=candidate.title,
                brand=candidate.brand,
                category=candidate.category,
                sub_category=candidate.sub_category or "",
                base_price=candidate.base_price,
                score=-best_distance,
                best_distance=best_distance,
                rerank_score=None,
                evidence=items,
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def _aggregate_reranked_by_product(
    reranked: list[RerankedChunk],
    candidates_by_id: dict[str, ProductCandidate],
) -> list[ProductHit]:
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
        candidate = candidates_by_id.get(pid)
        if candidate is None:
            continue
        leader = items[0]                # 已按 rerank_score 降序，第一个即最佳
        hits.append(
            ProductHit(
                product_id=pid,
                title=candidate.title,
                brand=candidate.brand,
                category=candidate.category,
                sub_category=candidate.sub_category or "",
                base_price=candidate.base_price,
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
    parser.add_argument("--no-rerank", action="store_true", help="不走 CrossEncoder 精排（快但质量低）。")
    args = parser.parse_args()

    service = SearchService(use_rerank=not args.no_rerank)
    result = service.search(args.query, top_k_products=args.top_k)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
