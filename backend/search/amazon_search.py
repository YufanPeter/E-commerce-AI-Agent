"""English, offline retrieval baseline for the Amazon snapshot."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from itertools import islice
from typing import Any

from rag.retriever import RetrievedChunk
from search.catalog_understanding import merge_turn, open_vocabulary, understand
from search.lexical import BM25, tokenize
from search.query_understanding import ParsedQuery
from search.search_service import ProductHit, SearchResult
from store.product_store import ProductStore


_STOP = set("a an the i me my for with and or in on to find show recommend buy need want please only under below up budget more around look can you ones anything product products".split())


class AmazonSearchService:
    """Select eligible catalog products before ranking; browse leaves without an API key."""

    def __init__(self, store: ProductStore | None = None) -> None:
        self._store = store or ProductStore()
        with closing(self._store.connect()) as conn:
            self._rows = conn.execute(
                """SELECT p.*, a.category_path, a.rating_number, d.marketing_description AS description
                   FROM products p JOIN amazon_product_metadata a USING (product_id)
                   JOIN product_descriptions d USING (product_id)
                   WHERE p.status = 'active' ORDER BY p.product_id"""
            ).fetchall()
        self._vocabulary = open_vocabulary(self._store)
        self._bm25 = BM25([
            tokenize(" ".join((row["title"], row["title"], row["brand"], row["description"])))
            for row in self._rows
        ])

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
        allowed_types = set(self._vocabulary.expand([intent.sub_category])) if intent.sub_category else None
        denied_types = set(self._vocabulary.expand(intent.sub_category_exclude))
        allowed = {brand.casefold() for brand in intent.brand_include}
        denied = {brand.casefold() for brand in intent.brand_exclude}
        preferred = {brand.casefold() for brand in profile.get("brand_include") or []}
        minimum, maximum = intent.min_price, intent.max_price
        if not (tokens or intent.sub_category or intent.category or allowed):
            intent = replace(intent, needs_clarification=True)
        if intent.unavailable_terms or intent.needs_clarification:
            return SearchResult(intent, [], 0, 0)
        ranking = []
        for i, row in enumerate(self._rows):
            brand, label, price = row["brand"].casefold(), row["sub_category"], row["base_price"]
            if (intent.category and row["category"] != intent.category) or (allowed_types is not None and label not in allowed_types):
                continue
            if label in denied_types or row["category"] in intent.category_exclude:
                continue
            if brand in denied or (allowed and brand not in allowed):
                continue
            if (minimum is not None and price < minimum) or (maximum is not None and price > maximum):
                continue
            score = self._bm25.score(tokens, i) if tokens else 0.0
            if score > 0 or intent.sub_category or intent.category or allowed:
                ranking.append((-(score + (0.25 if brand in preferred else 0.0)), -row["rating_number"], i))
        ranking.sort()
        # A leaf can hold a mislabeled listing, so an excluded type is also checked by title.
        selected = (
            entry for entry in ranking
            if not denied_types or self._vocabulary.match_types(self._rows[entry[2]]["title"])[0] not in denied_types
        )
        hits = [self._hit(self._rows[i], -score) for score, _, i in islice(selected, top_k_products)]
        return SearchResult(intent, hits, len(ranking), len(ranking))

    @staticmethod
    def _hit(row: Any, score: float) -> ProductHit:
        meta = {k: row[k] for k in ("product_id", "title", "brand", "category", "sub_category", "base_price", "image_url", "category_path")}
        meta.update(chunk_type="marketing_description", dataset="amazon_reviews_2023")
        evidence = RetrievedChunk(f"{row['product_id']}:marketing", row["description"], meta, None)
        return ProductHit(
            row["product_id"], row["title"], row["brand"], row["category"], row["sub_category"],
            row["base_price"], score, None, None, [evidence],
            min_price=row["base_price"], max_price=row["base_price"], sku_count=0,
        )
