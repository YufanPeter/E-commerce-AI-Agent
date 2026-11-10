"""Hybrid retrieval for the Amazon catalog.

SQLite decides what is eligible. BM25, product vectors and review vectors only propose
candidates, so a stale or fuzzy index can never surface a product that fails a filter.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import logging
import math
import os
from typing import Any

from catalog_config import AMAZON_INDEX_DIR, AMAZON_PRODUCT_COLLECTION, AMAZON_REVIEW_COLLECTION
from rag.reranker import get_reranker
from rag.retriever import RetrievedChunk
from rag.vector_index import ChromaVectorIndex, Condition, VectorIndex
from search.catalog_understanding import merge_turn, open_vocabulary, semantic_query, understand
from search.lexical import BM25, rrf_fuse, tokenize
from search.query_understanding import ParsedQuery
from search.search_service import ProductHit, SearchResult, _apply_profile_boost, _env_use_rerank
from search.where_builder import build_filters
from store.product_store import ProductStore


logger = logging.getLogger(__name__)

_STOP = set("a an the i me my for with and or in on to find show recommend buy need want please only under below up budget more around look can you ones anything product products".split())
_CHANNEL_SIZE = 100
_RERANK_POOL = 30
_MAX_REVIEWS_PER_PRODUCT = 2
_MIN_REVIEW_RATING = 3
_RATING_WEIGHT = 0.05
_CONFIRMED_ATTRIBUTE_BOOST = 1.15
_WEAK_MATCH_DISTANCE = float(os.getenv("CATALOG_WEAK_MATCH_DISTANCE", "0.47"))
_ATTRIBUTE_SYNONYMS = {"perfume": "fragrance"}


def _env_review_recall() -> bool:
    return os.getenv("CARTPILOT_REVIEW_RECALL", "1").strip() == "1"


def _open_index(collection: str) -> VectorIndex | None:
    """A catalog without a built index still serves keyword search."""
    if not AMAZON_INDEX_DIR.exists():
        return None
    try:
        index = ChromaVectorIndex(AMAZON_INDEX_DIR, collection)
        return index if index.count() else None
    except Exception as exc:  # noqa: BLE001 - vector search is optional
        logger.warning("Vector index %s is unavailable: %r", collection, exc)
        return None


class CatalogSearchService:
    """Fuse lexical, product and review recall over the eligible catalog subset."""

    def __init__(
        self,
        store: ProductStore | None = None,
        product_index: VectorIndex | None = None,
        review_index: VectorIndex | None = None,
        reranker: Any | None = None,
    ) -> None:
        self._store = store or ProductStore()
        with closing(self._store.connect()) as conn:
            self._rows = conn.execute(
                """SELECT p.*, a.category_path, a.rating_number, a.average_rating, a.product_role, a.dup_key,
                          d.marketing_description AS description,
                          r.min_price, r.max_price, r.sku_count
                   FROM products p JOIN amazon_product_metadata a USING (product_id)
                   JOIN product_descriptions d USING (product_id)
                   JOIN product_price_ranges r USING (product_id)
                   WHERE p.status = 'active' ORDER BY p.product_id"""
            ).fetchall()
            attribute_rows = conn.execute(
                "SELECT product_id, attribute, value FROM product_attributes "
                "WHERE attribute IN ('contains', 'free_of') AND status = 'observed'"
            ).fetchall()
        self._position = {row["product_id"]: i for i, row in enumerate(self._rows)}
        self._attributes: dict[tuple[str, str], set[str]] = {}
        for row in attribute_rows:
            self._attributes.setdefault((row["product_id"], row["attribute"]), set()).add(row["value"])
        self._vocabulary = open_vocabulary(self._store)
        self._bm25 = BM25([
            tokenize(" ".join((row["title"], row["title"], row["brand"], row["description"])))
            for row in self._rows
        ])
        self._product_index = product_index if product_index is not None else _open_index(AMAZON_PRODUCT_COLLECTION)
        self._review_index = review_index if review_index is not None else _open_index(AMAZON_REVIEW_COLLECTION)
        self._reranker = reranker

    def search(
        self, user_query: str, top_k_chunks: int = 50, top_k_products: int = 10,
        base: ParsedQuery | None = None, user_profile: dict[str, Any] | None = None,
        parsed: ParsedQuery | None = None,
    ) -> SearchResult:
        query = user_query.strip()
        profile = user_profile or {}
        if profile.get("personalization_enabled") is False:
            profile = {}
        intent = merge_turn(understand(query, self._vocabulary), base, profile)
        tokens = list(dict.fromkeys(t for t in tokenize(intent.retrieval_query) if t not in _STOP and not t.isdigit()))
        if not (tokens or intent.sub_category or intent.category or intent.brand_include):
            intent = replace(intent, needs_clarification=True)
        if intent.unavailable_terms or intent.needs_clarification:
            return SearchResult(intent, [], 0, 0)

        eligible = self._eligible(intent)
        lexical = self._lexical_ranking(tokens, eligible, intent)
        text = semantic_query(intent)
        products, reviews = self._vector_recall(text, intent)
        if self._is_weak_match(intent, products):
            return SearchResult(replace(intent, unavailable_terms=[query]), [], 0, 0)
        evidence = self._review_evidence(reviews, eligible)
        fused = rrf_fuse([lexical, [c.product_id for c in products], list(evidence)])
        pool = [pid for pid in fused if pid in eligible and self._passes_title_check(pid, intent)]
        checks = {pid: self._attribute_checks(pid, intent.excluded_attributes) for pid in pool}
        pool = [pid for pid in pool if checks[pid] is not None]

        distances = {c.product_id: c.distance for c in products}
        hits = self._ranked_hits(pool, text, distances, evidence, checks)
        hits, signals = _apply_profile_boost(hits, intent, profile)
        hits = self._without_duplicates(hits)[:top_k_products]
        return SearchResult(intent, hits, len(fused), len(pool), personalization=signals)

    # ----- eligibility -----------------------------------------------------------------

    def _eligible(self, intent: ParsedQuery) -> set[str]:
        types = set(self._vocabulary.expand([intent.sub_category])) if intent.sub_category else None
        denied_types = set(self._vocabulary.expand(intent.sub_category_exclude))
        allowed = {brand.casefold() for brand in intent.brand_include}
        denied = {brand.casefold() for brand in intent.brand_exclude}
        out: set[str] = set()
        for row in self._rows:
            label, brand = row["sub_category"], row["brand"].casefold()
            if (types is None and row["product_role"] != "primary") or (types is not None and label not in types):
                continue
            if (intent.category and row["category"] != intent.category) or row["category"] in intent.category_exclude:
                continue
            if label in denied_types or brand in denied or (allowed and brand not in allowed):
                continue
            if intent.max_price is not None and row["min_price"] > intent.max_price:
                continue  # No option is within the budget.
            if intent.min_price is not None and row["max_price"] < intent.min_price:
                continue  # No option reaches the budget floor.
            out.add(row["product_id"])
        return out

    def _passes_title_check(self, product_id: str, intent: ParsedQuery) -> bool:
        """A leaf can hold a mislabeled listing, so the title gets a say too."""
        title_type = self._vocabulary.match_types(self._rows[self._position[product_id]]["title"])[0]
        if title_type is None or title_type == intent.sub_category:
            return True
        if title_type in self._vocabulary.expand(intent.sub_category_exclude):
            return False
        wanted = set(self._vocabulary.expand([intent.sub_category])) if intent.sub_category else set()
        return title_type in wanted or title_type not in self._vocabulary.accessory_labels

    def _attribute_checks(self, product_id: str, excluded: list[str]) -> dict[str, str] | None:
        """``None`` means the listing states a rejected ingredient; absent evidence stays unknown."""
        contains = self._attributes.get((product_id, "contains"), set())
        free_of = self._attributes.get((product_id, "free_of"), set())
        checks: dict[str, str] = {}
        for term in excluded:
            canonical = _ATTRIBUTE_SYNONYMS.get(term, term)
            if canonical in contains:
                return None
            checks[term] = "free" if canonical in free_of else "unknown"
        return checks

    # ----- recall ----------------------------------------------------------------------

    def _lexical_ranking(self, tokens: list[str], eligible: set[str], intent: ParsedQuery) -> list[str]:
        ranking = []
        for product_id in eligible:
            position = self._position[product_id]
            score = self._bm25.score(tokens, position) if tokens else 0.0
            if score > 0 or intent.sub_category or intent.category or intent.brand_include:
                ranking.append((-score, -self._rows[position]["rating_number"], position))
        ranking.sort()
        return [self._rows[position]["product_id"] for _, _, position in ranking[:_CHANNEL_SIZE]]

    def _vector_filters(self, intent: ParsedQuery) -> list[Condition]:
        filters = build_filters(intent)
        return filters if intent.sub_category else [*filters, Condition("product_role", "eq", "primary")]

    def _vector_recall(self, text: str, intent: ParsedQuery) -> tuple[list[RetrievedChunk], list[RetrievedChunk]]:
        """Each channel fails alone, so a model outage degrades to keyword search."""
        if not text:
            return [], []
        filters = self._vector_filters(intent)
        products: list[RetrievedChunk] = []
        reviews: list[RetrievedChunk] = []
        try:
            if self._product_index is not None:
                products = self._product_index.search(text, _CHANNEL_SIZE, filters)
            if self._review_index is not None and _env_review_recall():
                reviews = self._review_index.search(
                    text, _CHANNEL_SIZE, [*filters, Condition("rating", "gte", _MIN_REVIEW_RATING)],
                )
        except Exception as exc:  # noqa: BLE001 - vector recall is optional
            logger.warning("Vector recall failed, continuing with what was retrieved: %r", exc)
        return products, reviews

    @staticmethod
    def _review_evidence(reviews: list[RetrievedChunk], eligible: set[str]) -> dict[str, list[RetrievedChunk]]:
        """Group review hits by product in rank order, capping how much one product counts."""
        grouped: dict[str, list[RetrievedChunk]] = {}
        for chunk in reviews:
            if chunk.product_id in eligible:
                entries = grouped.setdefault(chunk.product_id, [])
                if len(entries) < _MAX_REVIEWS_PER_PRODUCT:
                    entries.append(chunk)
        return grouped

    @staticmethod
    def _is_weak_match(intent: ParsedQuery, products: list[RetrievedChunk]) -> bool:
        """With no structured anchor, a distant nearest neighbor means the catalog lacks it."""
        if _WEAK_MATCH_DISTANCE <= 0 or not products or products[0].distance is None:
            return False
        if intent.sub_category or intent.category or intent.brand_include:
            return False
        return products[0].distance > _WEAK_MATCH_DISTANCE

    # ----- ranking ---------------------------------------------------------------------

    def _rating_prior(self, row: Any) -> float:
        """Nudge ties toward well-rated products, trusting ratings more as counts grow."""
        confidence = min(1.0, math.log10(row["rating_number"] + 1) / 3)
        return 1.0 + _RATING_WEIGHT * (float(row["average_rating"] or 4.0) - 4.0) * confidence

    def _rerank_scores(self, text: str, pool: list[str]) -> dict[str, float]:
        if not text or not pool or not _env_use_rerank():
            return {}
        documents = []
        for product_id in pool:
            row = self._rows[self._position[product_id]]
            documents.append(RetrievedChunk(product_id, f"{row['title']}\n{row['description']}", {"product_id": product_id}, None))
        try:
            if self._reranker is None:
                self._reranker = get_reranker()
            return {item.chunk.chunk_id: item.rerank_score for item in self._reranker.rerank(text, documents)}
        except Exception as exc:  # noqa: BLE001 - reranking only refines the fused order
            logger.warning("Rerank failed, keeping fused order: %r", exc)
            return {}

    def _ranked_hits(
        self, pool: list[str], text: str, distances: dict[str, float | None],
        evidence: dict[str, list[RetrievedChunk]], checks: dict[str, dict[str, str] | None],
    ) -> list[ProductHit]:
        rerank = self._rerank_scores(text, pool[:_RERANK_POOL])
        hits = []
        for rank, product_id in enumerate(pool, start=1):
            row = self._rows[self._position[product_id]]
            # Items beyond the rerank pool keep a fused score below every reranked item.
            base = rerank.get(product_id, 0.0 if rerank else 1.0 / (60 + rank))
            confirmed = checks[product_id] and all(v == "free" for v in checks[product_id].values())
            score = base * self._rating_prior(row) * (_CONFIRMED_ATTRIBUTE_BOOST if confirmed else 1.0)
            hits.append(self._hit(row, score, distances.get(product_id), rerank.get(product_id), evidence.get(product_id, []), checks[product_id] or {}))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits

    def _without_duplicates(self, hits: list[ProductHit]) -> list[ProductHit]:
        seen: set[str] = set()
        unique = []
        for hit in hits:
            key = self._rows[self._position[hit.product_id]]["dup_key"]
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            unique.append(hit)
        return unique

    @staticmethod
    def _hit(
        row: Any, score: float, distance: float | None, rerank_score: float | None,
        reviews: list[RetrievedChunk], checks: dict[str, str],
    ) -> ProductHit:
        meta = {k: row[k] for k in ("product_id", "title", "brand", "category", "sub_category", "base_price", "image_url", "category_path")}
        meta.update(chunk_type="marketing_description", dataset="amazon_reviews_2023")
        listing = RetrievedChunk(f"{row['product_id']}:marketing", row["description"], meta, distance)
        return ProductHit(
            row["product_id"], row["title"], row["brand"], row["category"], row["sub_category"],
            row["base_price"], score, distance, rerank_score, [listing, *reviews],
            min_price=row["min_price"], max_price=row["max_price"], sku_count=row["sku_count"],
            attribute_checks=checks,
        )
