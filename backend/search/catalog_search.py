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
import re
from typing import Any

from catalog_config import AMAZON_INDEX_DIR, AMAZON_PRODUCT_COLLECTION, AMAZON_REVIEW_COLLECTION
from graph.graph_store import GraphHit, GraphQuery, GraphStore, open_graph_store
from i18n import en as english
from rag.reranker import get_reranker
from rag.retriever import RetrievedChunk
from rag.vector_index import ChromaVectorIndex, Condition, VectorIndex
from search.catalog_understanding import merge_turn, open_vocabulary, semantic_query, understand
from search.lexical import BM25, rrf_fuse, tokenize
from search.query_understanding import ParsedQuery
from search.search_service import ProductHit, SearchResult, _apply_profile_boost, _env_use_rerank
from search.where_builder import build_filters
from store.attribute_rules import WANTED_ATTRIBUTES
from store.numeric_attributes import describe, numeric_attributes, satisfies
from store.product_store import ProductStore
from store.review_text import clean_review_text


logger = logging.getLogger(__name__)

_STOP = set("a an the i me my for with and or in on to find show recommend buy need want please only under below up budget more around look can you ones anything product products".split())
_CHANNEL_SIZE = 100
_RERANK_POOL = 30
_MAX_REVIEWS_PER_PRODUCT = 2
_MIN_REVIEW_RATING = 3
_RATING_WEIGHT = 0.05
# A listing that confirms every condition gains this share of its relevance score. Relevance
# still leads: a confirmed but off-target product cannot pass a clearly better match.
_CONFIRMED_WEIGHT = float(os.getenv("CATALOG_CONFIRMED_WEIGHT", "0.3"))
_RERANK_REVIEW_CHARS = 300
_COMPLAINT_MAX_RATING = 2
_COMPLAINT_MIN_CHARS = 20
_WEAK_MATCH_DISTANCE = float(os.getenv("CATALOG_WEAK_MATCH_DISTANCE", "0.47"))
_ATTRIBUTE_SYNONYMS = {"perfume": "fragrance"}
# "iphone_15", "iphone_15_pro_max": a model of a device family.
_DEVICE_MODEL = re.compile(r"^(?P<family>[a-z]+)_(?P<number>\d+)(?:_(?P<variant>.+))?$")
# Product types shaped for one model: a case for the iPhone 14 does not fit an iPhone 15. A cable or
# charger that lists some models usually works with others, so a mismatch there only ranks lower.
_MODEL_SHAPED = re.compile(r"\bcases?\b|\bscreen protectors?\b|\blens protectors?\b|\bskins?\b", re.I)
_NO_VARIANT = r"(?!\s*(?:pro|plus|max|mini)\b)"
# The rest of a sentence that denies fit: "NOT fit iPhone 13 Mini (5.4"), iPhone 13 5G (6.1")".
# A decimal point ("5.4") does not end the sentence.
_DENIAL = re.compile(
    r"\b(?:not|n't)\s+(?:fit|fits|compatible with|work with|works with|for|suitable for)\s+((?:[^.;\n]|\.\d)+)", re.I,
)
# "Fits most smartphones" is universal fit; "works with most phone cases" is about cases, not phones.
_UNIVERSAL_FIT = re.compile(
    r"\buniversal (?:fit|size|phone case|case)\b"
    r"|\b(?:fits?|for|compatible with|works with|designed for)\s+(?:all|most|any)\s+(?:cell\s+)?"
    r"(?:phones?|smartphones?|iphones?)\b(?!\s*(?:cases?|covers?))",
    re.I,
)


def _brand_prefixes(brand: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", brand.casefold())
    return ["".join(words[:end]) for end in range(1, len(words) + 1)]


def same_brand(stored: str, named: str) -> bool:
    """Whether a listing's brand is the brand the shopper named.

    Sellers spell one brand several ways ("MAYBELLINE", "Maybelline New York", "L'Oreal
    Paris"), so a brand matches when one name is the other's leading words; a different
    word ("Applebee's" for "Apple") does not.
    """
    ours, theirs = _brand_prefixes(stored), _brand_prefixes(named)
    return bool(ours and theirs) and (ours[-1] in theirs or theirs[-1] in ours)


def _confirmed_count(checks: dict[str, str]) -> int:
    """How many of the shopper's conditions the listing itself confirms."""
    return sum(value in ("free", "meets") for value in checks.values())


def _confirmation_factor(checks: dict[str, str] | None) -> float:
    """Score multiplier for the share of conditions the listing confirms."""
    if not checks:
        return 1.0
    return 1.0 + _CONFIRMED_WEIGHT * _confirmed_count(checks) / len(checks)


def _review_body(chunk: RetrievedChunk) -> str:
    """Review text without the title prefix the review index adds."""
    document = chunk.document or ""
    return clean_review_text(document.split("Review:", 1)[1] if "Review:" in document else document)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").casefold()) if w not in english.NAME_STOPWORDS and len(w) > 2}


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
        graph: GraphStore | None = None,
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
            checked = ("contains", "free_of", "compatible_device", *WANTED_ATTRIBUTES, *numeric_attributes())
            attribute_rows = conn.execute(
                "SELECT product_id, attribute, value FROM product_attributes "
                f"WHERE attribute IN ({', '.join('?' * len(checked))}) AND status = 'observed'",
                checked,
            ).fetchall()
        self._position = {row["product_id"]: i for i, row in enumerate(self._rows)}
        self._attributes: dict[tuple[str, str], set[str]] = {}
        for row in attribute_rows:
            self._attributes.setdefault((row["product_id"], row["attribute"]), set()).add(row["value"])
        self._vocabulary = open_vocabulary(self._store)
        self._bm25 = BM25([
            tokenize(" ".join((row["title"], row["title"], row["brand"], row["description"])), model_numbers=True)
            for row in self._rows
        ])
        self._product_index = product_index if product_index is not None else _open_index(AMAZON_PRODUCT_COLLECTION)
        self._review_index = review_index if review_index is not None else _open_index(AMAZON_REVIEW_COLLECTION)
        self._reranker = reranker
        self.graph = graph if graph is not None else open_graph_store()

    @property
    def vocabulary(self) -> Any:
        return self._vocabulary

    def min_price(self, product_id: str) -> float | None:
        position = self._position.get(product_id)
        return None if position is None else self._rows[position]["min_price"]

    def hits_for(self, product_ids: list[str], max_price: float | None = None, popular_first: bool = False) -> list[ProductHit]:
        """Current facts for graph-proposed products; unknown IDs and over-budget items drop out."""
        rows = [self._rows[self._position[pid]] for pid in product_ids if pid in self._position]
        rows = [row for row in rows if max_price is None or row["min_price"] <= max_price]
        if popular_first:
            rows.sort(key=lambda row: -row["rating_number"])
        return self._without_duplicates([self._hit(row, 0.0, None, None, [], {}) for row in rows])

    def search(
        self, user_query: str, top_k_chunks: int = 50, top_k_products: int = 10,
        base: ParsedQuery | None = None, user_profile: dict[str, Any] | None = None,
        parsed: ParsedQuery | None = None,
    ) -> SearchResult:
        query = user_query.strip()
        profile = user_profile or {}
        if profile.get("personalization_enabled") is False:
            profile = {}
        # A caller that already holds the structured request ("show more", a search retried
        # without its budget) passes it whole; reading the text again would bring back the
        # very condition the caller dropped.
        current = parsed if parsed is not None else understand(query, self._vocabulary, base=base)
        intent = merge_turn(current, None if parsed is not None else base, profile)
        tokens = list(dict.fromkeys(
            t for t in tokenize(intent.retrieval_query, model_numbers=True) if t not in _STOP and not t.isdigit()
        ))
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
        graph_hits = self._graph_recall(intent)
        fused = rrf_fuse([
            lexical, [c.product_id for c in products], list(evidence), [h.product_id for h in graph_hits],
            self._confirmed_recall(eligible, intent),
        ])
        pool = [pid for pid in fused if pid in eligible and self._passes_title_check(pid, intent)]
        avoided = [str(term).casefold() for term in profile.get("avoid_terms") or []]
        avoid_signals = [
            {"kind": "avoid_term", "value": term} for term in avoided
            if any(_ATTRIBUTE_SYNONYMS.get(term, term) in self._attributes.get((pid, "contains"), set()) for pid in pool)
        ]
        checks = {pid: self._attribute_checks(pid, intent, avoided) for pid in pool}
        pool = [pid for pid in pool if checks[pid] is not None]

        distances = {c.product_id: c.distance for c in products}
        claims = {h.product_id: h.evidence for h in graph_hits}
        hits = self._ranked_hits(pool, text, distances, evidence, checks, claims)
        # A product type names no category, but a remembered budget is kept per category.
        hits, signals = _apply_profile_boost(hits, replace(intent, category=intent.category or self._category_of(intent.sub_category)), profile)
        hits = self._with_complaints(self._without_duplicates(hits)[:top_k_products], text)
        return SearchResult(intent, hits, len(fused), len(pool), personalization=[*signals, *avoid_signals])

    def _category_of(self, product_type: str | None) -> str | None:
        """Top-level category of a leaf or group label."""
        leaves = self._vocabulary.expand([product_type]) if product_type else []
        return self._vocabulary.labels.get(leaves[0]) if leaves else None

    # ----- eligibility -----------------------------------------------------------------

    def _eligible(self, intent: ParsedQuery) -> set[str]:
        types = set(self._vocabulary.expand([intent.sub_category])) if intent.sub_category else None
        denied_types = set(self._vocabulary.expand(intent.sub_category_exclude))
        allowed, denied = intent.brand_include, intent.brand_exclude
        out: set[str] = set()
        for row in self._rows:
            label, brand = row["sub_category"], row["brand"]
            if (types is None and row["product_role"] != "primary") or (types is not None and label not in types):
                continue
            if (intent.category and row["category"] != intent.category) or row["category"] in intent.category_exclude:
                continue
            if label in denied_types:
                continue
            if denied and any(same_brand(brand, name) for name in denied):
                continue
            if allowed and not any(same_brand(brand, name) for name in allowed):
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

    def _attribute_checks(self, product_id: str, intent: ParsedQuery, avoided: list[str] = ()) -> dict[str, str] | None:
        """``None`` means the listing contradicts a condition; absent evidence stays unknown.

        A rejected ingredient is "free" when the listing says so, and a measured limit is
        "meets" when a stated value satisfies it. ``avoided`` comes from saved memory and
        applies across categories, so it only removes products that state the ingredient.
        """
        contains = self._attributes.get((product_id, "contains"), set())
        free_of = self._attributes.get((product_id, "free_of"), set())
        if any(_ATTRIBUTE_SYNONYMS.get(term, term) in contains for term in avoided):
            return None
        checks = self._device_checks(product_id, intent.compatible_with)
        if checks is None:
            return None
        for term in intent.excluded_attributes:
            canonical = _ATTRIBUTE_SYNONYMS.get(term, term)
            if canonical in contains:
                return None
            checks[term] = "free" if canonical in free_of else "unknown"
        # A listing stated free of a remembered ingredient says so on its card, so the reply
        # can tell those apart. Silent listings get no mark: memory applies to every search,
        # and "fragrance not confirmed" on headphones would be noise.
        for term in avoided:
            if _ATTRIBUTE_SYNONYMS.get(term, term) in free_of:
                checks.setdefault(term, "free")
        for constraint in intent.numeric:
            met = satisfies(self._attributes.get((product_id, constraint["attribute"]), ()), constraint["op"], constraint["value"])
            if met is False:
                return None
            checks[describe(constraint)] = "meets" if met else "unknown"
        stated = set().union(*(self._attributes.get((product_id, name), set()) for name in WANTED_ATTRIBUTES))
        for value in intent.features:
            checks[value.replace("_", " ")] = "meets" if value in stated else "unknown"
        return checks

    def _device_checks(self, product_id: str, wanted: list[str]) -> dict[str, str] | None:
        """Compatibility with the shopper's device; ``None`` when a case or protector is for another phone.

        Product types shaped for one model must name the shopper's model, or claim universal fit.
        For other types a listing's model list is rarely complete, so a mismatch only ranks lower.
        """
        if not wanted:
            return {}
        stated = self._attributes.get((product_id, "compatible_device"), set())
        models = [device for device in wanted if _DEVICE_MODEL.match(device)]
        row = self._rows[self._position[product_id]]
        shaped = row["sub_category"] in self._vocabulary.accessory_labels and _MODEL_SHAPED.search(row["sub_category"])
        checks: dict[str, str] = {}
        full = f"{row['title']} {row['description']}"
        for device in models or wanted:
            label = "works with " + device.replace("_", " ")
            model = _DEVICE_MODEL.match(device)
            if model is not None:
                # A case's title is what it was made for; descriptions often list other sizes loosely.
                title_names_models = re.search(rf"\b{re.escape(model['family'])}\s*\d", row["title"], re.I)
                if shaped and title_names_models:
                    named = self._names_model(row["title"], model)
                else:
                    named = device in stated or self._names_model(full, model)
                denied = self._denies_model(full, model)
                named = named and not denied
            else:
                denied = False
                named = device in stated or any(value.startswith(device + "_") for value in stated)
            checks[label] = "meets" if named else "unknown"
            if not named and model is not None and shaped and (denied or not _UNIVERSAL_FIT.search(full)):
                return None  # Made for some other phone, or never says which.
        return checks

    @staticmethod
    def _model_pattern(model: re.Match[str]) -> str:
        # "iPhone 14 Pro Max" needs that variant; plain "iPhone 14" must not be a Pro, Plus or Max.
        variant = model["variant"]
        suffix = (r"\s*" + r"[\s-]*".join(map(re.escape, variant.split("_"))) + r"\b") if variant else _NO_VARIANT
        return rf"(?<![\d.])\b{model['number']}\b(?!\.\d){suffix}"

    @classmethod
    def _denies_model(cls, text: str, model: re.Match[str]) -> bool:
        """"Does NOT fit iPhone 13 Mini, iPhone 13 or 13 Pro" denies every model in that sentence."""
        for denial in _DENIAL.finditer(text):
            if re.search(rf"\b{re.escape(model['family'])}\b[^.;\n]{{0,40}}?{cls._model_pattern(model)}", denial[1], re.I):
                return True
        return False

    @classmethod
    def _names_model(cls, text: str, model: re.Match[str]) -> bool:
        """Whether the text names this model, including lists such as "iPhone 14/15"."""
        near = rf"\b{re.escape(model['family'])}\b[^.;\n]{{0,60}}?{cls._model_pattern(model)}"
        return re.search(near, text, re.I) is not None

    def _confirmed_recall(self, eligible: set[str], intent: ParsedQuery) -> list[str]:
        """Products whose listing confirms a condition, so a deep match is not left out of the pool."""
        if not (intent.excluded_attributes or intent.numeric or intent.features or intent.compatible_with):
            return []
        if not (intent.sub_category or intent.category):
            return []
        ranked = []
        for product_id in eligible:
            confirmed = _confirmed_count(self._attribute_checks(product_id, intent) or {})
            if confirmed:
                ranked.append((-confirmed, -self._rows[self._position[product_id]]["rating_number"], product_id))
        return [product_id for *_, product_id in sorted(ranked)[:_CHANNEL_SIZE]]

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

    def _graph_recall(self, intent: ParsedQuery) -> list[GraphHit]:
        if self.graph is None:
            return []
        labels = self._vocabulary.expand([intent.sub_category]) if intent.sub_category else []
        return self.graph.recall(
            GraphQuery(tuple(intent.aspects), tuple(intent.use_cases), tuple(intent.compatible_with), tuple(labels)),
            _CHANNEL_SIZE,
        )

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

    def _rerank_scores(
        self, text: str, pool: list[str], evidence: dict[str, list[RetrievedChunk]] | None = None,
    ) -> dict[str, float]:
        """Score the listing together with the reviews that matched the request.

        Reviews carry what listings leave out ("stays put on runs"), so they reach the final order.
        """
        if not text or not pool or not _env_use_rerank():
            return {}
        documents = []
        for product_id in pool:
            row = self._rows[self._position[product_id]]
            reviews = [_review_body(chunk)[:_RERANK_REVIEW_CHARS] for chunk in (evidence or {}).get(product_id, [])]
            body = f"{row['title']}\n{row['description']}"
            if any(reviews):
                body += "\nCustomer reviews: " + " | ".join(review for review in reviews if review)
            documents.append(RetrievedChunk(product_id, body, {"product_id": product_id}, None))
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
        claims: dict[str, tuple[dict[str, Any], ...]],
    ) -> list[ProductHit]:
        # Confirmed products deeper in the fused list are reranked too, or they would all score zero.
        confirmed_later = [pid for pid in pool[_RERANK_POOL:] if _confirmed_count(checks[pid] or {})]
        rerank = self._rerank_scores(text, pool[:_RERANK_POOL] + confirmed_later[:_RERANK_POOL], evidence)
        hits = []
        for rank, product_id in enumerate(pool, start=1):
            row = self._rows[self._position[product_id]]
            # Items beyond the rerank pool keep a fused score below every reranked item.
            base = rerank.get(product_id, 0.0 if rerank else 1.0 / (60 + rank))
            score = base * self._rating_prior(row) * _confirmation_factor(checks[product_id])
            hits.append(self._hit(row, score, distances.get(product_id), rerank.get(product_id), evidence.get(product_id, []), checks[product_id] or {}, claims.get(product_id, ())))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits

    def _with_complaints(self, hits: list[ProductHit], text: str) -> list[ProductHit]:
        """Attach each product's most relevant low-rated review so its card can name a drawback.

        Review recall keeps only ratings of 3 and up, so complaints come from the catalog here.
        Shipping and seller complaints say nothing about the product and are skipped.
        """
        if not hits:
            return hits
        ids = [hit.product_id for hit in hits]
        try:
            with closing(self._store.connect()) as conn:
                rows = conn.execute(
                    f"""SELECT product_id, source_index, rating, content FROM product_reviews
                        WHERE product_id IN ({', '.join('?' * len(ids))})
                          AND (rating <= ? OR polarity = 'negative') AND length(content) >= ?""",
                    (*ids, _COMPLAINT_MAX_RATING, _COMPLAINT_MIN_CHARS),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001 - a card without a drawback is still a card
            logger.warning("Could not load complaints: %r", exc)
            return hits
        wanted = _words(text)
        best: dict[str, tuple[tuple[int, int, int], Any]] = {}
        for row in rows:
            content = clean_review_text(row["content"])
            if english.SERVICE_COMPLAINT_RE.search(content):
                continue
            key = (len(_words(content) & wanted), -row["rating"], min(len(content), 300))
            if row["product_id"] not in best or key > best[row["product_id"]][0]:
                best[row["product_id"]] = (key, row)
        out = []
        for hit in hits:
            found = best.get(hit.product_id)
            if found is None:
                out.append(hit)
                continue
            row = found[1]
            complaint = RetrievedChunk(
                f"{hit.product_id}:review:{row['source_index']}", row["content"],
                {"product_id": hit.product_id, "chunk_type": "user_review", "polarity": "negative",
                 "rating": int(row["rating"]), "dataset": "amazon_reviews_2023"},
                None,
            )
            out.append(replace(hit, evidence=[*hit.evidence, complaint]))
        return out

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
        reviews: list[RetrievedChunk], checks: dict[str, str], claims: tuple[dict[str, Any], ...] = (),
    ) -> ProductHit:
        meta = {k: row[k] for k in ("product_id", "title", "brand", "category", "sub_category", "base_price", "image_url", "category_path")}
        meta.update(chunk_type="marketing_description", dataset="amazon_reviews_2023")
        listing = RetrievedChunk(f"{row['product_id']}:marketing", row["description"], meta, distance)
        return ProductHit(
            row["product_id"], row["title"], row["brand"], row["category"], row["sub_category"],
            row["base_price"], score, distance, rerank_score, [listing, *reviews],
            min_price=row["min_price"], max_price=row["max_price"], sku_count=row["sku_count"],
            attribute_checks=checks, graph_evidence=claims,
            rating={"average": row["average_rating"], "count": row["rating_number"]} if row["rating_number"] else None,
        )
