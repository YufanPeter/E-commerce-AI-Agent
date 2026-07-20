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

import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

import logging

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
    """Complete result of one retrieval request."""

    parsed: ParsedQuery
    hits: list[ProductHit]
    raw_chunk_count: int                 # Chroma chunks before deduplication/filtering.
    filtered_chunk_count: int            # Chunks remaining after postfiltering.

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
    ) -> SearchResult:
        """Run the end-to-end search pipeline.

        ``top_k_chunks`` controls the reranking candidate pool and ``top_k_products`` the
        final product count. ``base`` contains prior structured intent for refinement.
        """
        # 1. Understand intent with caching.
        parsed = understand_query(user_query)

        # 1.5. Merge current constraints with prior intent before clarification checks.
        if base is not None:
            parsed = parsed.merge_base(base)

        parsed = _apply_user_profile(parsed, user_profile)

        if parsed.needs_clarification:
            return SearchResult(parsed=parsed, hits=[], raw_chunk_count=0, filtered_chunk_count=0)

        # 2. Hard filters and vector retrieval. Price is excluded from Chroma because one
        # base price cannot represent whether any SKU satisfies the budget.
        where_parsed = replace(parsed, min_price=None, max_price=None)
        where = build_chroma_where(where_parsed)
        chunks = self._retriever.search(
            query=parsed.retrieval_query or user_query,
            top_k=top_k_chunks,
            where=where,
        )
        raw_count = len(chunks)

        # 2.5. When an inferred subcategory yields too few products, retry at parent-category
        # level. Never relax a subcategory stated literally by the user, and preserve all
        # other brand, category, and exclusion constraints.
        if parsed.sub_category and where is not None:
            sub = parsed.sub_category
            literal = sub in (user_query or "") or sub in (parsed.original_query or "")
            distinct = len({c.product_id for c in chunks if c.product_id})
            if not literal and distinct < top_k_products:
                relaxed = replace(where_parsed, sub_category=None)
                relaxed_chunks = self._retriever.search(
                    query=parsed.retrieval_query or user_query,
                    top_k=top_k_chunks,
                    where=build_chroma_where(relaxed),
                )
                relaxed_distinct = len({c.product_id for c in relaxed_chunks if c.product_id})
                if relaxed_distinct > distinct:
                    chunks = relaxed_chunks
                    raw_count = len(chunks)

        # 2.6. Run BM25 over the already compliant vector candidate subset and fuse with
        # vector order before reranking. This keeps structured constraints authoritative.
        if self._use_hybrid and chunks:
            chunks = self._hybrid_reorder_chunks(
                query=parsed.retrieval_query or user_query,
                chunks=chunks,
            )

        # 3. Apply exclusions at product level before reranking. If any chunk or metadata
        # matches an exclusion, remove every chunk for that product.
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
                )
            }
            if banned_products:
                chunks = [c for c in chunks if c.product_id not in banned_products]

        # 3.5. Apply budget bounds against true SKU price ranges.
        if (parsed.max_price is not None or parsed.min_price is not None) and chunks:
            chunks = self._price_filter_chunks(chunks, parsed.min_price, parsed.max_price)
        filtered_count = len(chunks)

        # 4. API reranking. Configuration, network, and response errors remain visible.
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
    """Merge long-lived Preference into ParsedQuery without overriding this turn."""
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

    soft_terms = list(parsed.soft_terms or [])
    preference_terms = list(user_profile.get("preference_keywords") or [])
    preference_terms.extend(user_profile.get("style_tags") or [])
    for term in preference_terms:
        if isinstance(term, str) and term and term not in soft_terms:
            soft_terms.append(term)

    category = parsed.category
    favorites = [c for c in (user_profile.get("favorite_categories") or []) if isinstance(c, str)]
    if category is None and not parsed.sub_category and len(favorites) == 1:
        category = favorites[0]

    min_price = parsed.min_price
    max_price = parsed.max_price
    if min_price is None and user_profile.get("budget_min") is not None:
        min_price = _coerce_float(user_profile.get("budget_min"))
    if max_price is None and user_profile.get("budget_max") is not None:
        max_price = _coerce_float(user_profile.get("budget_max"))

    retrieval_terms = [parsed.retrieval_query or parsed.original_query]
    retrieval_terms.extend(term for term in soft_terms if term not in retrieval_terms)

    return ParsedQuery(
        original_query=parsed.original_query,
        intent=parsed.intent,
        category=category,
        sub_category=parsed.sub_category,
        category_exclude=parsed.category_exclude,
        sub_category_exclude=parsed.sub_category_exclude,
        max_price=max_price,
        min_price=min_price,
        brand_include=brand_include,
        brand_exclude=brand_exclude,
        negative_ingredients=parsed.negative_ingredients,
        soft_terms=soft_terms,
        retrieval_query=" ".join(t for t in retrieval_terms if t),
        needs_clarification=parsed.needs_clarification,
    )


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


def _chunk_is_banned(
    chunk: RetrievedChunk,
    negatives: list[str],
    excluded_subs: set[str],
    excluded_cats: set[str],
    excluded_brands: set[str],
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
    haystack = " ".join(
        [
            chunk.document or "",
            str(meta.get("title", "")),
            sub,
            cat,
        ]
    )
    return _negative_hit(haystack, negatives)


def _expanded_brand_set(brands: list[str]) -> set[str]:
    if not brands:
        return set()
    return {_normalize_brand_key(brand) for brand in expand_brands(brands)}


def _normalize_brand_key(brand: str) -> str:
    return (brand or "").strip().lower().replace(" ", "")


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
# Singleton and CLI
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_search_service() -> SearchService:
    """Return the process-wide service; call once during FastAPI startup to warm it."""
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
