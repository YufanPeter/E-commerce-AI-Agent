"""English, offline retrieval baseline for the Amazon snapshot."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import re
from typing import Any

from catalog_config import AMAZON_CATEGORIES
from rag.retriever import RetrievedChunk
from search.lexical import BM25, tokenize
from search.query_understanding import ParsedQuery
from search.search_service import ProductHit, SearchResult
from store.product_store import ProductStore


@dataclass(frozen=True)
class Cell:
    terms: str
    paths: tuple[str, ...]

    def matches(self, path: str) -> bool:
        return any(path == tail or path.endswith(" > " + tail) for tail in self.paths)


CELLS = {
    "Smartwatches": Cell("smart watches?|smartwatches?|fitness watches?|sports watches?|apple watch", ("Smartwatches",)),
    "Smartwatch Bands": Cell("smart ?watch bands?|watch straps?|watch bands?|apple watch bands?", ("Smartwatch Bands",)),
    "Laptop Stands": Cell("laptop stands?|notebook stands?", ("Laptop Accessories > Stands",)),
    "Headphones": Cell("headphones|earbuds|earphones|headsets", ("Earbud Headphones", "Over-Ear Headphones")),
    "Earbud Cases": Cell("earbud cases?|headphone cases?|earbuds cases?|headphones cases?", ("Headphones, Earbuds & Accessories > Cases",)),
    "Phone Cases": Cell("phone cases?|iphone cases?|smartphone cases?", ("Cases, Holsters & Sleeves > Basic Cases", "Cases, Holsters & Sleeves > Flip Cases")),
    "Facial Sunscreens": Cell("facial sunscreen|face sunscreen|sunscreen for (?:the |my )?face", ("Facial Sunscreens",)),
    "Body Sunscreens": Cell("body sunscreen|sunscreen for (?:the |my )?body", ("Body Sunscreens",)),
    "Sunscreens": Cell("sunscreens?|sunblock", ("Facial Sunscreens", "Body Sunscreens")),
    "Face Washes": Cell("face wash(?:es)?|facial cleansers?", ("Face > Cleansers > Washes",)),
    "Shampoos": Cell("shampoos?", ("Shampoos",)),
    "Serums": Cell("facial serums?|face serums?|serums?", ("Serums",)),
    "Running Shoes": Cell("road running shoes?|running shoes?|jogging shoes?", ("Running > Road Running",)),
    "Dresses": Cell("dresses|dress", ("Clothing > Dresses", "Dresses > Casual")),
    "Coffee Machines": Cell("coffee machines?|coffee makers?", ("Coffee Makers > Coffee Machines",)),
    "Skillets": Cell("skillets?|frying pans?|non[- ]?stick pans?", ("Pots & Pans > Skillets",)),
    "Bedding Sets": Cell("bedding sets?|sheet sets?", ("Sheet & Pillowcase Sets",)),
    "Outside the catalog": Cell("smartphones?|phones?|cat food|dog food|blood pressure monitors?|windshield wipers?|yoga mats?", ()),
}
_CELL_PATTERNS = {key: re.compile(r"\b(?:" + cell.terms + r")\b", re.I) for key, cell in CELLS.items()}
_NEGATIVE = re.compile(r"\b(?:no|not|exclude|avoid|without|except|instead of|don't want)\s*$", re.I)
_AMOUNT = r"(\d+(?:,\d{3})*(?:\.\d{1,2})?)"
_RANGE = re.compile(r"\bbetween\s+\$?\s*" + _AMOUNT + r"\s+(?:and|to)\s+\$?\s*" + _AMOUNT, re.I)
_MAX = re.compile(r"\b(?:under|below|up to|at most|less than|no more than|within|budget(?:\s+(?:of|to|is|at|around))?)\s*\$?\s*" + _AMOUNT, re.I)
_MIN = re.compile(r"\b(?:over|above|at least|more than|from)\s*\$?\s*" + _AMOUNT, re.I)
_OTHER_CURRENCY = re.compile(r"[€£¥￥]|\b(?:EUR|GBP|CAD|CNY|RMB)\b", re.I)
_NO_BUDGET = re.compile(r"\b(?:no budget|any price|no price limit|clear (?:the )?budget)\b", re.I)
_ANY_BRAND = re.compile(r"\b(?:any brand|all brands|clear (?:the )?brand filter)\b", re.I)
_STOP = set("a an the i me my for with and or in on to find show recommend buy need want please only under below up budget more around look can you ones anything product products".split())


def _cells(text: str) -> tuple[str | None, list[str]]:
    matches = sorted(
        ((m.start(), m.end(), key) for key, pattern in _CELL_PATTERNS.items() for m in pattern.finditer(text)),
        key=lambda hit: (-(hit[1] - hit[0]), hit[0]),
    )
    selected = []
    for start, end, key in matches:
        if not any(start < right and end > left for left, right, _ in selected):
            selected.append((start, end, key))
    positive, excluded = [], []
    for start, _, key in sorted(selected):
        (excluded if _NEGATIVE.search(text[:start]) else positive).append(key)
    return (positive[0] if positive else None), list(dict.fromkeys(excluded))


def _amount(match: re.Match[str] | None, group: int = 1) -> float | None:
    return float(match[group].replace(",", "")) if match else None


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
        self._brands = {row["brand"].casefold(): row["brand"] for row in self._rows if row["brand"]}
        self._brand_names = sorted(self._brands, key=len, reverse=True)
        self._bm25 = BM25([
            tokenize(" ".join((row["title"], row["title"], row["brand"], row["description"])))
            for row in self._rows
        ])

    def _explicit_brands(self, query: str) -> tuple[list[str], list[str]]:
        included, excluded = [], []
        cues = re.finditer(r"\b(only|by|no|not|exclude|avoid|without|except)\s+", query, re.I)
        for cue in cues:
            tail = query[cue.end():].casefold()
            matches = (b for b in self._brand_names if tail.startswith(b) and
                       (len(tail) == len(b) or not tail[len(b)].isalnum()))
            brand = next(matches, None)
            if brand is not None:
                (included if cue[1].lower() in {"only", "by"} else excluded).append(self._brands[brand])
        return included, excluded

    def search(
        self, user_query: str, top_k_chunks: int = 50, top_k_products: int = 10,
        base: ParsedQuery | None = None, user_profile: dict[str, Any] | None = None,
        parsed: ParsedQuery | None = None,
    ) -> SearchResult:
        query = user_query.strip()
        cell, excluded_cells = _cells(query)
        category = next((label for label in AMAZON_CATEGORIES.values() if label.casefold() in query.casefold()), None)
        profile = user_profile or {}
        if profile.get("personalization_enabled") is False:
            profile = {}
        switch = bool(base and ((cell and cell != base.sub_category) or (category and category != base.category)))
        if base and not switch:
            cell = cell or base.sub_category
            category = category or base.category
            excluded_cells = list(dict.fromkeys(base.sub_category_exclude + excluded_cells))
        bounds = _RANGE.search(query)
        minimum = _amount(bounds) if bounds else _amount(_MIN.search(query))
        maximum = _amount(bounds, 2) if bounds else _amount(_MAX.search(query))
        if not _NO_BUDGET.search(query):
            minimum = minimum if minimum is not None else (base.min_price if base else profile.get("budget_min"))
            maximum = maximum if maximum is not None else (base.max_price if base else profile.get("budget_max"))
        included, excluded = self._explicit_brands(query)
        if not _ANY_BRAND.search(query):
            denied = {b.casefold() for b in excluded}
            if not included:
                included = [b for b in (base.brand_include if base else [])
                            if b.casefold() not in denied]
            excluded += list(base.brand_exclude if base else profile.get("brand_exclude") or [])
        allowed = {b.casefold() for b in included}
        denied = {b.casefold() for b in excluded} - allowed
        preferred = {b.casefold() for b in profile.get("brand_include") or []}
        terms = f"{query} {base.retrieval_query}" if base and not switch else query
        tokens = list(dict.fromkeys(t for t in tokenize(terms) if t not in _STOP and not t.isdigit()))
        unavailable = ["Requested product type"] if cell == "Outside the catalog" else []
        if _OTHER_CURRENCY.search(query):
            unavailable.append("This catalog only supports USD budgets")
        intent = ParsedQuery(
            original_query=query, category=category, sub_category=cell,
            sub_category_exclude=excluded_cells, brand_include=included,
            brand_exclude=[b for b in dict.fromkeys(excluded) if b.casefold() in denied],
            min_price=minimum, max_price=maximum, retrieval_query=terms,
            needs_clarification=not bool(tokens or cell or category or included),
            unavailable_terms=unavailable,
        )
        if unavailable or intent.needs_clarification:
            return SearchResult(intent, [], 0, 0)
        ranking = []
        for i, row in enumerate(self._rows):
            brand, path, price = row["brand"].casefold(), row["category_path"], row["base_price"]
            if (category and row["category"] != category) or (cell and cell in CELLS and not CELLS[cell].matches(path)):
                continue
            if any(key in CELLS and CELLS[key].matches(path) for key in excluded_cells):
                continue
            if brand in denied or (allowed and brand not in allowed):
                continue
            if (minimum is not None and price < minimum) or (maximum is not None and price > maximum):
                continue
            score = self._bm25.score(tokens, i) if tokens else 0.0
            if score > 0 or cell or category or allowed:
                ranking.append((-(score + (0.25 if brand in preferred else 0.0)), -row["rating_number"], i))
        ranking.sort()
        hits = [self._hit(self._rows[i], -score) for score, _, i in ranking[:top_k_products]]
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
