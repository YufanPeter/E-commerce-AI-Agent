from __future__ import annotations

"""End-to-end retrieval from a natural-language query to ranked product hits.

Hybrid retrieval pipeline:

    用户原句
        │
        ▼  1. Intent understanding
    understand_query(q) → ParsedQuery
        │
        ▼  2. Hard filters and vector retrieval
    ChromaRetriever.search(retrieval_query, where=build_chroma_where(parsed))
        → List[RetrievedChunk] ordered by cosine distance
        │
        ▼  3. Postfilter and product-level aggregation
    Remove disallowed products and merge chunks by product ID
        │
        ▼
    List[ProductHit]

The service is reused across requests, retains evidence chunks for explainability, and
leaves business ranking such as sales, ratings, and personalization to higher layers.
"""

import logging
import os
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from rag.retriever import ChromaRetriever, RetrievedChunk
from rag.reranker import ApiReranker, RerankedChunk, get_reranker
from search.query_understanding import ParsedQuery, expand_brands, understand_query
from search.where_builder import build_chroma_where

logger = logging.getLogger(__name__)


def _env_use_rerank() -> bool:
    """Return whether API reranking is enabled by ``USE_RERANK``.

    It is enabled by default and disabled by 0, false, no, off, or an empty value.
    """
    raw = os.getenv("USE_RERANK")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_use_hybrid() -> bool:
    """Return whether BM25/vector hybrid retrieval is enabled by ``USE_HYBRID``.

    It is enabled by default. BM25 runs over the structured-filter subset and fuses with
    dense retrieval through RRF.
    """
    raw = os.getenv("USE_HYBRID")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class ProductHit:
    """Product-level hit aggregating all retrieved chunks for that product."""

    product_id: str
    title: str
    brand: str
    category: str
    sub_category: str
    base_price: float
    # Higher-is-better product score: reranking score or negative best distance.
    score: float
    best_distance: float | None         # Original Chroma distance for diagnostics.
    rerank_score: float | None          # Raw reranking score, if enabled.
    evidence: list[RetrievedChunk] = field(default_factory=list)
    # SKU 价格区间（来自 chunk 元数据；老索引没有时退回 base_price）
    min_price: float | None = None
    max_price: float | None = None
    sku_count: int = 0
    # Rejected attribute → "free" (listing says so) or "unknown" (listing is silent).
    attribute_checks: dict[str, str] = field(default_factory=dict)

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
            "min_price": self.min_price,
            "max_price": self.max_price,
            "sku_count": self.sku_count,
            "evidence_count": len(self.evidence),
            "evidence_chunk_types": [c.chunk_type for c in self.evidence],
        }


@dataclass(frozen=True)
class SearchResult:
    """Complete result of one retrieval request."""

    parsed: ParsedQuery
    hits: list[ProductHit]
    raw_chunk_count: int                 # Chroma 原始返回的 chunk 数（去重前）
    filtered_chunk_count: int            # 经 negative_ingredients 后置过滤剩余的 chunk 数
    # 本次检索实际用到的长期记忆，给话术和页面说明「为什么这样推荐」
    personalization: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parsed": self.parsed.to_dict(),
            "raw_chunk_count": self.raw_chunk_count,
            "filtered_chunk_count": self.filtered_chunk_count,
            "hits": [hit.to_dict() for hit in self.hits],
        }


class SearchService:
    """Combine query understanding, retrieval, reranking, filtering, and aggregation."""

    def __init__(
        self,
        retriever: ChromaRetriever | None = None,
        reranker: ApiReranker | None = None,
        use_rerank: bool | None = None,
        product_store: Any | None = None,
        use_hybrid: bool | None = None,
    ) -> None:
        # Tests may inject mock retrievers and rerankers.
        self._retriever = retriever or ChromaRetriever()
        # Environment controls reranking when no explicit argument is provided.
        self._use_rerank = _env_use_rerank() if use_rerank is None else use_rerank
        # Environment controls hybrid retrieval when no explicit argument is provided.
        self._use_hybrid = _env_use_hybrid() if use_hybrid is None else use_hybrid
        # Create the reranking HTTP client lazily on first search.
        self._reranker = reranker
        # Load product facts lazily for SKU-range price filtering.
        self._products = product_store

    def _get_products(self) -> Any:
        if self._products is None:
            from store.product_store import ProductStore
            self._products = ProductStore()
        return self._products

    def _get_reranker(self) -> ApiReranker:
        if self._reranker is None:
            self._reranker = get_reranker()
        return self._reranker

    def _price_filter_chunks(
        self,
        chunks: list[RetrievedChunk],
        min_price: float | None,
        max_price: float | None,
    ) -> list[RetrievedChunk]:
        """Filter chunks by SKU price ranges rather than one base price.

        A product remains when at least one SKU satisfies the budget. Fact lookup failures
        leave retrieval results unchanged.
        """
        product_ids = [c.product_id for c in chunks if c.product_id]
        if not product_ids:
            return chunks
        try:
            candidates = self._get_products().get_products_by_ids(list(dict.fromkeys(product_ids)))
        except Exception as exc:  # noqa: BLE001 - price filtering is optional
            logger.warning("Failed to load price ranges; skipping the price post-filter: %r", exc)
            return chunks
        ok: set[str] = set()
        for cand in candidates:
            pr = cand.price_range
            if max_price is not None and pr.min_price > max_price:
                continue  # No SKU is at or below the budget ceiling.
            if min_price is not None and pr.max_price < min_price:
                continue  # No SKU reaches the budget floor.
            ok.add(cand.product_id)
        return [c for c in chunks if c.product_id in ok]

    def _product_fulltext(self, product_ids: list[str]) -> dict[str, str]:
        """Load candidate BM25 text from title, marketing description, and FAQ."""
        if not product_ids:
            return {}
        placeholders = ",".join("?" for _ in product_ids)
        out: dict[str, str] = {}
        try:
            with self._get_products().connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT p.product_id AS pid, p.title AS title,
                           COALESCE(d.marketing_description, '') AS descr,
                           COALESCE((
                               SELECT group_concat(f.question || ' ' || f.answer, ' ')
                               FROM product_faqs f WHERE f.product_id = p.product_id
                           ), '') AS faqs
                    FROM products p
                    LEFT JOIN product_descriptions d ON d.product_id = p.product_id
                    WHERE p.product_id IN ({placeholders})
                    """,
                    list(product_ids),
                ).fetchall()
            for r in rows:
                out[r["pid"]] = " ".join([r["title"] or "", r["descr"] or "", r["faqs"] or ""])
        except Exception as exc:  # noqa: BLE001 - hybrid retrieval is optional
            logger.warning("Failed to load the BM25 corpus; skipping hybrid fusion: %r", exc)
        return out

    def _hybrid_reorder_chunks(
        self,
        query: str,
        chunks: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        """Fuse BM25 and vector rankings over the filtered subset with RRF.

        The first chunk occurrence defines vector product order, BM25 ranks full candidate
        text, and RRF produces a stable product order while preserving within-product
        chunk order.
        """
        from search.lexical import rank_ids_bm25, rrf_fuse

        # Product-level vector order from first chunk occurrence.
        vec_order: list[str] = []
        seen: set[str] = set()
        for c in chunks:
            pid = c.product_id
            if pid and pid not in seen:
                seen.add(pid)
                vec_order.append(pid)
        if len(vec_order) <= 1:
            return chunks

        # Sparse order over candidate product text.
        texts = self._product_fulltext(vec_order)
        if not texts:
            return chunks
        ids = [pid for pid in vec_order if pid in texts]
        bm25_order = rank_ids_bm25(query, ids, [texts[pid] for pid in ids])

        fused = rrf_fuse([vec_order, bm25_order])
        rank_of = {pid: i for i, pid in enumerate(fused)}
        # Stable reorder by fused product rank, leaving absent products last.
        fallback = len(rank_of)
        return sorted(chunks, key=lambda c: rank_of.get(c.product_id, fallback))

    def search(
        self,
        user_query: str,
        top_k_chunks: int = 50,
        top_k_products: int = 10,
        base: ParsedQuery | None = None,
        user_profile: dict[str, Any] | None = None,
        parsed: ParsedQuery | None = None,
    ) -> SearchResult:
        """Run the end-to-end search pipeline.

        parsed: 调用方已经拿到的结构化意图（如图搜时由 VLM 短语确定性构造），
                传入则跳过 LLM query 解析，省一次往返。

        top_k_chunks: Chroma 召回的 chunk 数量。rerank 时要多召（默认 50）
                      给 API 精排选择余地；不 rerank 时 30 也够。
        top_k_products: 聚合后返回的商品数量。
        base: 上一轮结构化检索意图。非空表示这是一次"细化"——把本轮解析叠加到
              base 上（见 ParsedQuery.merge_base），避免丢失品类/价格等上下文。
        """
        # ① 意图理解（带缓存；orchestrator 通常已并行预取）
        t0 = time.perf_counter()
        if parsed is None:
            parsed = understand_query(user_query)
        t_understand = time.perf_counter() - t0

        # 1.5. Merge current constraints with prior intent before clarification checks.
        if base is not None:
            parsed = parsed.merge_base(base)

        parsed = _apply_user_profile(parsed, user_profile)

        if parsed.needs_clarification or parsed.unavailable_terms:
            return SearchResult(parsed=parsed, hits=[], raw_chunk_count=0, filtered_chunk_count=0)

        # ② 硬过滤 + 向量召回
        where = build_chroma_where(parsed)
        t1 = time.perf_counter()
        # 多取一些：下面每个商品限条数后才够 top_k_chunks 条，否则候选会被评价多的商品占满。
        chunks = self._retriever.search(
            query=parsed.retrieval_query or user_query,
            top_k=top_k_chunks * _CHUNK_POOL_MULTIPLIER,
            where=where,
        )
        t_retrieve = time.perf_counter() - t1
        raw_count = len(chunks)

        # 只猜到一级类目、相似度又弱：大概率是库里没有的东西（「机械键盘」），不拿手机顶替。
        if _is_weak_category_guess(parsed, chunks):
            parsed = replace(parsed, unavailable_terms=[parsed.original_query or user_query])
            return SearchResult(parsed=parsed, hits=[], raw_chunk_count=raw_count, filtered_chunk_count=0)

        # 2.6. Run BM25 over the already compliant vector candidate subset and fuse with
        # vector order before reranking. This keeps structured constraints authoritative.
        if self._use_hybrid and chunks:
            chunks = self._hybrid_reorder_chunks(
                query=parsed.retrieval_query or user_query,
                chunks=chunks,
            )

        # ③ 后置过滤：product 级剔除否定项（在 rerank 之前做，节省 rerank 算力）。
        #    注意必须是 **product 级** 而非 chunk 级：一个商品有多个 chunk（描述/规格/
        #    评价/FAQ），若只剔"文本含否定词的那个 chunk"，其余 chunk 仍会让整个商品
        #    被召回——这正是"说了不要功能饮料还出现"的根因。这里只要某商品任一 chunk
        #    的文本或其 title/品类命中否定词，就把该商品的**全部** chunk 一起剔除。
        #
        #    两道防线（P0 的 where $nin 已在召回阶段挡掉结构化品类反选，这里是兜底）：
        #    a) sub_category_exclude / category_exclude：按 chunk metadata 的品类字段
        #       兜底剔除（防 SQLite 回退路径或 metadata 写法差异导致 $nin 漏网）。
        #    b) negative_ingredients：先经别名归一（能量饮料→功能饮料），再扫
        #       chunk 文本 + title + 品类做子串剔除（处理成分/口味等非品类否定）。
        negatives = _normalize_negatives(parsed.negative_ingredients)
        excluded_subs = set(parsed.sub_category_exclude or [])
        excluded_cats = set(parsed.category_exclude or [])
        excluded_brands = _expanded_brand_set(parsed.brand_exclude)
        if negatives or excluded_subs or excluded_cats or excluded_brands:
            banned_products = {
                c.product_id
                for c in chunks
                if c.product_id and _chunk_is_banned(
                    c,
                    negatives,
                    excluded_subs,
                    excluded_cats,
                    excluded_brands,
                    strict_terms=set((user_profile or {}).get("avoid_terms") or []),
                )
            }
            if banned_products:
                chunks = [c for c in chunks if c.product_id not in banned_products]
        chunks = _limit_chunks_per_product(chunks)[:top_k_chunks]

        # 3.5. Apply budget bounds against true SKU price ranges.
        if (parsed.max_price is not None or parsed.min_price is not None) and chunks:
            chunks = self._price_filter_chunks(chunks, parsed.min_price, parsed.max_price)
        filtered_count = len(chunks)

        # ④ API 精排。客户端层负责百炼 qwen3-rerank → 智谱的自动 fallback；
        # 两边都失败时向上抛错，避免静默给出未经精排的不同语义结果。
        t2 = time.perf_counter()
        if self._use_rerank and chunks:
            reranked = self._get_reranker().rerank(
                query=parsed.retrieval_query or user_query,
                chunks=chunks,
            )
            hits = _aggregate_reranked_by_product(reranked)
        else:
            hits = _aggregate_by_distance(chunks)
        hits, personalization = _apply_profile_boost(hits, parsed, user_profile)
        hits = hits[:top_k_products]
        personalization = _profile_hard_signals(parsed, user_profile) + personalization
        logger.info(
            "search: understand=%dms retrieve=%dms rerank=%dms chunks=%d→%d hits=%d",
            int(t_understand * 1000), int(t_retrieve * 1000), int((time.perf_counter() - t2) * 1000),
            raw_count, filtered_count, len(hits),
        )

        return SearchResult(
            parsed=parsed,
            hits=hits,
            raw_chunk_count=raw_count,
            filtered_chunk_count=filtered_count,
            personalization=personalization,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle and needle in text for needle in needles)


# Prefixes that turn a term occurrence into an explicit free-from declaration.
_FREE_DECL_PREFIXES = ("无", "0", "０", "零", "不含", "无添加", "不添加", "未添加", "不加")


def _negative_hit(text: str, needles: list[str]) -> bool:
    """Return whether text contains a disallowed term without a free-from declaration.

    A matching free-from phrase takes precedence over naive substring matching, preventing
    compliant products from being removed merely because they mention the ingredient.
    """
    for needle in needles:
        if not needle or needle not in text:
            continue
        free_decls = [prefix + needle for prefix in _FREE_DECL_PREFIXES]
        if any(decl in text for decl in free_decls):
            continue  # An explicit free-from declaration keeps the product.
        return True
    return False


def _apply_user_profile(
    parsed: ParsedQuery,
    user_profile: dict[str, Any] | None,
) -> ParsedQuery:
    """把长期偏好里的【硬约束】并入本轮检索，不覆盖用户这句话明说的内容。

    只有用户明确说过不要的东西才是硬约束：品牌排除、成分忌讳。预算、喜欢的品牌、关注品类
    都是软信号，走 ``_apply_profile_boost`` 排序加权——「平时预算 500」不能让用户
    今天搜笔记本一条结果都没有。
    """
    if not user_profile:
        return parsed
    if user_profile.get("personalization_enabled") is False:
        return parsed

    brand_include = list(parsed.brand_include or [])
    brand_exclude = list(parsed.brand_exclude or [])
    profile_exclude = [
        b for b in (user_profile.get("brand_exclude") or [])
        if isinstance(b, str) and not _brand_conflicts_with_include(b, brand_include)
    ]
    for brand in profile_exclude:
        if brand not in brand_exclude:
            brand_exclude.append(brand)

    negatives = list(parsed.negative_ingredients or [])
    for term in user_profile.get("avoid_terms") or []:
        if isinstance(term, str) and term and term not in negatives:
            negatives.append(term)

    soft_terms = list(parsed.soft_terms or [])
    preference_terms = list(user_profile.get("preference_keywords") or [])
    preference_terms.extend(user_profile.get("style_tags") or [])
    for term in preference_terms:
        if isinstance(term, str) and term and term not in soft_terms:
            soft_terms.append(term)

    retrieval_terms = [parsed.retrieval_query or parsed.original_query]
    retrieval_terms.extend(term for term in soft_terms if term not in retrieval_terms)

    return replace(
        parsed,
        brand_exclude=brand_exclude,
        negative_ingredients=negatives,
        soft_terms=soft_terms,
        retrieval_query=" ".join(t for t in retrieval_terms if t),
    )


# 软信号权重。相关性分数归一到 [0, 1] 后叠加，相关性始终是主项。
_BOOST_BRAND_LIKE = float(os.getenv("MEMORY_BOOST_BRAND", "0.20"))
_BOOST_BRAND_INFERRED = float(os.getenv("MEMORY_BOOST_BRAND_INFERRED", "0.08"))
_BOOST_BUDGET = float(os.getenv("MEMORY_BOOST_BUDGET", "0.35"))
_BOOST_CATEGORY = float(os.getenv("MEMORY_BOOST_CATEGORY", "0.05"))
_BOOST_PRICE_TIER = float(os.getenv("MEMORY_BOOST_PRICE_TIER", "0.10"))
_MIN_RELEVANCE_SPAN = 0.4


def _profile_budget_for(
    parsed: ParsedQuery,
    user_profile: dict[str, Any],
) -> tuple[float | None, float | None, str]:
    """取这次检索适用的预算：子类目 > 一级类目 > 全局。返回 (下限, 上限, 作用域)。"""
    specific = user_profile.get("category_specific") or {}
    for scope in (parsed.sub_category, parsed.category):
        bucket = specific.get(scope) if scope else None
        if isinstance(bucket, dict) and (bucket.get("budget_min") is not None or bucket.get("budget_max") is not None):
            return _coerce_float(bucket.get("budget_min")), _coerce_float(bucket.get("budget_max")), str(scope)
    return _coerce_float(user_profile.get("budget_min")), _coerce_float(user_profile.get("budget_max")), ""


def _budget_fit(hit: ProductHit, low: float | None, high: float | None) -> float:
    """商品和长期预算的匹配度：全系都在预算内 1.0，有规格买得起 0.6，都买不起 0。"""
    lo_price = hit.min_price if hit.min_price is not None else hit.base_price
    hi_price = hit.max_price if hit.max_price is not None else hit.base_price
    if (high is not None and lo_price > high) or (low is not None and hi_price < low):
        return 0.0
    fully_inside = (high is None or hi_price <= high) and (low is None or lo_price >= low)
    return 1.0 if fully_inside else 0.6


def _apply_profile_boost(
    hits: list[ProductHit],
    parsed: ParsedQuery,
    user_profile: dict[str, Any] | None,
) -> tuple[list[ProductHit], list[dict[str, Any]]]:
    """按长期偏好的软信号重排，不增删商品。返回 (重排结果, 实际起作用的信号)。"""
    if not hits or not user_profile or user_profile.get("personalization_enabled") is False:
        return hits, []

    liked = {_normalize_brand_key(b) for b in expand_brands(list(user_profile.get("brand_include") or []))}
    inferred_brands = list((user_profile.get("_inferred") or {}).get("brand_like") or [])
    inferred = {_normalize_brand_key(b) for b in expand_brands(inferred_brands)}
    has_turn_price = parsed.max_price is not None or parsed.min_price is not None
    low, high, budget_scope = (None, None, "") if has_turn_price else _profile_budget_for(parsed, user_profile)
    has_budget = low is not None or high is not None
    favorites = set(user_profile.get("favorite_categories") or []) if not (parsed.category or parsed.sub_category) else set()
    tier = user_profile.get("price_tier") if user_profile.get("price_tier") in {"value", "premium"} else None

    scores = [float(h.score or 0.0) for h in hits]
    lo_score, hi_score = min(scores), max(scores)
    # 最小跨度：候选分差很小时（「推荐手机」10 款只差 0.1）差异只是噪声，不能拉伸成 0~1，
    # 否则偏好永远赢不了；分差大时相关性仍然是主项。
    span = max(hi_score - lo_score, _MIN_RELEVANCE_SPAN)
    prices = sorted(h.min_price if h.min_price is not None else h.base_price for h in hits)
    lo_price, hi_price = prices[0], prices[-1]
    price_span = (hi_price - lo_price) or 1.0

    used: dict[str, dict[str, Any]] = {}

    def _mark(kind: str, value: str, scope: str = "", effect: str = "soft") -> None:
        used[f"{kind}|{scope}|{value}"] = {"kind": kind, "scope": scope, "value": value, "effect": effect}

    def _score(hit: ProductHit) -> float:
        total = (float(hit.score or 0.0) - lo_score) / span
        brand_key = _normalize_brand_key(hit.brand)
        if brand_key in liked:
            total += _BOOST_BRAND_LIKE
            _mark("brand_like", hit.brand)
        elif brand_key in inferred:
            total += _BOOST_BRAND_INFERRED
            _mark("brand_like_inferred", hit.brand)
        if has_budget:
            total += _BOOST_BUDGET * _budget_fit(hit, low, high)
        if hit.category in favorites:
            total += _BOOST_CATEGORY
            _mark("category", hit.category)
        if tier:
            price = hit.min_price if hit.min_price is not None else hit.base_price
            position = (price - lo_price) / price_span
            total += _BOOST_PRICE_TIER * (position if tier == "premium" else 1.0 - position)
        return total

    ranked = sorted(hits, key=_score, reverse=True)
    if has_budget:
        _mark("budget", f"{low if low is not None else ''}-{high if high is not None else ''}", budget_scope)
    if tier:
        _mark("price_tier", tier)
    return ranked, list(used.values())


def _profile_hard_signals(
    parsed: ParsedQuery,
    user_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not user_profile or user_profile.get("personalization_enabled") is False:
        return []
    signals = [
        {"kind": "brand_avoid", "scope": "", "value": brand, "effect": "hard"}
        for brand in user_profile.get("brand_exclude") or []
        if brand in (parsed.brand_exclude or [])
    ]
    signals += [
        {"kind": "avoid_term", "scope": "", "value": term, "effect": "hard"}
        for term in user_profile.get("avoid_terms") or []
        if term in (parsed.negative_ingredients or [])
    ]
    return signals


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _brand_conflicts_with_include(brand: str, includes: list[str]) -> bool:
    left = brand.lower().replace(" ", "")
    for include in includes:
        right = include.lower().replace(" ", "")
        if left and right and (left in right or right in left):
            return True
    return False


# Map negative aliases to literal catalog terms so substring fallback remains effective.
# Structured category exclusions are already applied during retrieval.
_NEGATIVE_ALIAS: dict[str, str] = {
    "功能性饮料": "功能饮料",
    "能量饮料": "功能饮料",
    "功能型饮料": "功能饮料",
    "提神饮料": "功能饮料",
    "碳酸": "碳酸饮料",
    "汽水": "碳酸饮料",
    "气泡水": "碳酸饮料",
    "0糖": "无糖",
    "零糖": "无糖",
    "无糖型": "无糖",
}


def _normalize_negatives(negatives: list[str] | None) -> list[str]:
    """Normalize negative aliases to catalog terms and deduplicate safely."""
    if not negatives:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in negatives:
        term = _NEGATIVE_ALIAS.get(raw, raw)
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


# 相关性门槛：只对「没有子类目、也没有品牌」的检索生效。值取决于 embedding 模型和商品库，
# 换模型或扩库后要用 evals/run_recall.py 重新确认；设为 0 关闭。
_WEAK_MATCH_DISTANCE = float(os.getenv("SEARCH_WEAK_MATCH_DISTANCE", "0.44"))
# 用户本来就在逛大类（「推荐点吃的」），相似度低是正常的，不算库外。
_CATEGORY_BROWSE_WORDS: tuple[str, ...] = (
    "吃的", "喝的", "零食", "食品", "饮料", "美妆", "化妆品", "护肤", "数码", "电子",
    "衣服", "服饰", "服装", "运动", "随便", "推荐点", "有什么",
)


_CHUNK_POOL_MULTIPLIER = 3
_MAX_CHUNKS_PER_PRODUCT = 4


def _limit_chunks_per_product(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """每个商品最多留最相关的几条证据，把精排名额让给更多商品。保持原有相关性顺序。"""
    counts: dict[str, int] = {}
    kept: list[RetrievedChunk] = []
    for chunk in chunks:
        pid = chunk.product_id
        counts[pid] = counts.get(pid, 0) + 1
        if not pid or counts[pid] <= _MAX_CHUNKS_PER_PRODUCT:
            kept.append(chunk)
    return kept


def _is_weak_category_guess(parsed: ParsedQuery, chunks: list[RetrievedChunk]) -> bool:
    if _WEAK_MATCH_DISTANCE <= 0 or not chunks:
        return False
    if parsed.sub_category or parsed.brand_include:
        return False
    best = chunks[0]
    # 备用索引是另一个向量空间，距离不可比
    if best.distance is None or best.metadata.get("_index") == "fallback":
        return False
    if best.distance <= _WEAK_MATCH_DISTANCE:
        return False
    text = parsed.original_query or ""
    return not any(word in text for word in _CATEGORY_BROWSE_WORDS)


# 否定说法：出现在商品文本的词前面时，说明商品恰恰不含它（「无酒精」「不添加香精」）。
_TEXT_NEGATION_CUES: tuple[str, ...] = (
    "无", "不含", "不添加", "没有", "未添加", "不加", "零", "免", "拒绝", "不会", "告别", "去除",
)
# 这些词里的「糖」不代表商品含糖。
_TERM_COMPOUND_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "糖": ("控糖", "血糖", "低糖", "减糖", "抗糖", "代糖", "糖尿", "无糖", "0糖", "零糖", "糖化", "怕糖", "戒糖", "糖分焦虑"),
}


def _mentions_positively(text: str, term: str) -> bool:
    """商品文本是否真的声称含有这个词，而不是在说「不含」它。"""
    exceptions = _TERM_COMPOUND_EXCEPTIONS.get(term, ())
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        start = index + len(term)
        window = text[max(0, index - 8):index].replace("无论", "").replace("无法", "")
        # 「0」只有紧贴才算否定（0糖），否则「250ml 功能饮料」里的数字会被误判
        if any(cue in window for cue in _TEXT_NEGATION_CUES) or text[max(0, index - 1):index] == "0":
            continue
        if any(
            word in text[max(0, index - len(word) + 1):index + len(word)]
            for word in exceptions
        ):
            continue
        return True


def _chunk_is_banned(
    chunk: RetrievedChunk,
    negatives: list[str],
    excluded_subs: set[str],
    excluded_cats: set[str],
    excluded_brands: set[str],
    strict_terms: set[str] | None = None,
) -> bool:
    """Return whether the product represented by this chunk must be excluded.

    Brand, category, subcategory, or normalized free-text exclusion matches are sufficient.
    """
    meta = chunk.metadata
    sub = str(meta.get("sub_category", ""))
    cat = str(meta.get("category", ""))
    brand = str(meta.get("brand", ""))
    if brand and _normalize_brand_key(brand) in excluded_brands:
        return True
    if sub and sub in excluded_subs:
        return True
    if cat and cat in excluded_cats:
        return True
    if not negatives:
        return False
    # 评价和 FAQ 里常有对比说法（「比含糖茶饮清爽」「可以自己加糖浆」），不能当成分依据，
    # 只看商品自身描述。用户确认过的忌讳（过敏等）更严格，FAQ 里的说法也算。
    meta_text = " ".join([str(meta.get("title", "")), sub, cat])
    document = chunk.document or ""
    for term in negatives:
        if not term:
            continue
        if chunk.chunk_type == "user_review":
            scanned = meta_text
        elif chunk.chunk_type == "official_faq" and term not in (strict_terms or set()):
            scanned = meta_text
        else:
            scanned = f"{document} {meta_text}"
        if _mentions_positively(scanned, term):
            return True
    return False


def _expanded_brand_set(brands: list[str]) -> set[str]:
    if not brands:
        return set()
    return {_normalize_brand_key(brand) for brand in expand_brands(brands)}


def _normalize_brand_key(brand: str) -> str:
    return (brand or "").strip().lower().replace(" ", "")


def _price_fields(meta: dict[str, Any]) -> dict[str, Any]:
    base = float(meta.get("base_price", 0) or 0)
    lo = meta.get("min_price")
    hi = meta.get("max_price")
    return {
        "base_price": base,
        "min_price": float(lo) if lo is not None else base,
        "max_price": float(hi) if hi is not None else base,
        "sku_count": int(meta.get("sku_count", 0) or 0),
    }


def _aggregate_by_distance(chunks: list[RetrievedChunk]) -> list[ProductHit]:
    """Aggregate without reranking by the best Chroma distance per product.

    Chroma input order is retained. Product score is negative minimum distance so higher
    remains better across both ranking paths.
    """
    grouped: dict[str, list[RetrievedChunk]] = {}
    for chunk in chunks:
        pid = chunk.product_id
        if not pid:
            continue
        grouped.setdefault(pid, []).append(chunk)

    hits: list[ProductHit] = []
    for pid, items in grouped.items():
        leader = items[0]                # Chroma order puts the best distance first.
        meta = leader.metadata
        best_distance = float(leader.distance) if leader.distance is not None else float("inf")
        hits.append(
            ProductHit(
                product_id=pid,
                title=str(meta.get("title", "")),
                brand=str(meta.get("brand", "")),
                category=str(meta.get("category", "")),
                sub_category=str(meta.get("sub_category", "")),
                score=-best_distance,
                best_distance=best_distance,
                rerank_score=None,
                evidence=items,
                **_price_fields(meta),
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def _aggregate_reranked_by_product(reranked: list[RerankedChunk]) -> list[ProductHit]:
    """Aggregate reranked chunks by maximum score per product.

    Reranked input is descending, so the first chunk is the product's best evidence.

    Max pooling reflects the strongest matching evidence and avoids favoring products
    merely because they have more chunks.
    """
    grouped: dict[str, list[RerankedChunk]] = {}
    for r in reranked:
        pid = r.product_id
        if not pid:
            continue
        grouped.setdefault(pid, []).append(r)

    hits: list[ProductHit] = []
    for pid, items in grouped.items():
        leader = items[0]                # Highest reranking score appears first.
        meta = leader.chunk.metadata
        hits.append(
            ProductHit(
                product_id=pid,
                title=str(meta.get("title", "")),
                brand=str(meta.get("brand", "")),
                category=str(meta.get("category", "")),
                sub_category=str(meta.get("sub_category", "")),
                score=leader.rerank_score,
                best_distance=float(leader.chunk.distance) if leader.chunk.distance is not None else None,
                rerank_score=leader.rerank_score,
                evidence=[r.chunk for r in items],
                **_price_fields(meta),
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


# ---------------------------------------------------------------------------
# Singleton and CLI
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_search_service() -> SearchService:
    """Return the process-wide service; call once during FastAPI startup to warm it."""
    from catalog_config import PROFILE

    if PROFILE.search_backend == "catalog":
        from search.catalog_search import CatalogSearchService

        return CatalogSearchService()
    return SearchService()


def main() -> None:
    import argparse
    import json
    import logging
    import os

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))

    parser = argparse.ArgumentParser(description="End-to-end retrieval demo")
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--no-rerank", action="store_true", help="Skip API reranking for faster, lower-quality results")
    args = parser.parse_args()

    service = SearchService(use_rerank=not args.no_rerank)
    result = service.search(args.query, top_k_products=args.top_k)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
