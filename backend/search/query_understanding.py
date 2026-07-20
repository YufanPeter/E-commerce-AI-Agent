from __future__ import annotations

"""LLM-first query-understanding orchestration.

This module owns the data contract, orchestration, caching, and deterministic safeguards.
``search.llm_parser`` performs the primary natural-language-to-``ParsedQuery`` extraction.

When LLM parsing fails, retrieval keeps the raw query and omits inferred positive hard
filters. Small deterministic negative-category and brand safeguards preserve explicit
exclusions without attempting to replace full language understanding.

Dependency direction:
    ``llm_parser`` imports this module's contracts and taxonomy helpers; API and agent
    layers call only ``understand_query``.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "backend" / "storage" / "ecommerce_agent.sqlite3"


# ---------------------------------------------------------------------------
# 1. Data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedQuery:
    """Structured retrieval intent consumed by every downstream module."""

    original_query: str
    intent: str = "product_search"
    category: str | None = None
    sub_category: str | None = None
    category_exclude: list[str] = field(default_factory=list)
    sub_category_exclude: list[str] = field(default_factory=list)
    max_price: float | None = None
    min_price: float | None = None
    brand_include: list[str] = field(default_factory=list)
    brand_exclude: list[str] = field(default_factory=list)
    negative_ingredients: list[str] = field(default_factory=list)
    soft_terms: list[str] = field(default_factory=list)
    retrieval_query: str = ""
    needs_clarification: bool = False

    @property
    def hard_filters(self) -> dict[str, object]:
        """Return hard filters suitable for SQLite or Chroma."""
        return {
            "category": self.category,
            "sub_category": self.sub_category,
            "category_exclude": self.category_exclude,
            "sub_category_exclude": self.sub_category_exclude,
            "max_price": self.max_price,
            "min_price": self.min_price,
            "brand_include": self.brand_include,
            "brand_exclude": self.brand_exclude,
        }

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["hard_filters"] = self.hard_filters
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ParsedQuery":
        """Restore an instance from ``to_dict`` output.

        Unknown keys are ignored, especially the derived ``hard_filters`` property that
        is not a constructor argument.
        """
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def merge_base(self, base: "ParsedQuery") -> "ParsedQuery":
        """Overlay the current parsed turn on the previous structured intent.

        Scalar category and price fields override only when present. A non-empty brand
        inclusion replaces the old brand, while exclusions, negative ingredients, and
        soft preferences accumulate. Retrieval terms are merged with current terms first,
        and inherited context makes clarification unnecessary.
        """
        def pick(cur: object, prev: object) -> object:
            return cur if cur is not None else prev

        def union(cur: list, prev: list) -> list:
            return list(dict.fromkeys(list(prev or []) + list(cur or [])))

        def replace_or_keep(cur: list, prev: list) -> list:
            return list(cur) if cur else list(prev or [])

        tokens: list[str] = []
        for tok in (self.retrieval_query or "").split() + (base.retrieval_query or "").split():
            if tok and tok not in tokens:
                tokens.append(tok)
        retrieval_query = " ".join(tokens) or self.original_query or base.original_query

        return ParsedQuery(
            original_query=self.original_query or base.original_query,
            intent=self.intent or base.intent,
            category=pick(self.category, base.category),
            sub_category=pick(self.sub_category, base.sub_category),
            category_exclude=union(self.category_exclude, base.category_exclude),
            sub_category_exclude=union(self.sub_category_exclude, base.sub_category_exclude),
            max_price=pick(self.max_price, base.max_price),
            min_price=pick(self.min_price, base.min_price),
            brand_include=replace_or_keep(self.brand_include, base.brand_include),
            brand_exclude=union(self.brand_exclude, base.brand_exclude),
            negative_ingredients=union(self.negative_ingredients, base.negative_ingredients),
            soft_terms=union(self.soft_terms, base.soft_terms),
            retrieval_query=retrieval_query,
            needs_clarification=False,
        )


# ---------------------------------------------------------------------------
# 2. Taxonomy derived from SQLite for function-schema enums
# ---------------------------------------------------------------------------

# Canonical brand keys map to every SQLite/Chroma spelling, including the canonical name.
BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "Apple 苹果":      ("Apple 苹果", "苹果"),
    "耐克":            ("耐克", "Nike"),
    "The North Face":  ("The North Face", "北面"),
}

_EXTRA_BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "Apple 苹果": ("Apple", "apple", "iphone", "iPhone"),
}


@lru_cache(maxsize=1)
def load_taxonomy(db_path: Path = DEFAULT_DB_PATH) -> dict[str, object]:
    """Read distinct category, subcategory, and brand values from ``products``.

    The result maps subcategories to parents, exposes canonical brands for the LLM enum,
    and expands canonical brands to stored variants. It is cached once per process.
    """
    if not db_path.exists():
        return {"sub_to_cat": {}, "brands": [], "brand_expand": {}}

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT category, sub_category, brand FROM products"
        ).fetchall()

    sub_to_cat: dict[str, str] = {}
    raw_brands: set[str] = set()
    for category, sub_category, brand in rows:
        if sub_category:
            sub_to_cat[sub_category] = category
        if brand:
            raw_brands.add(brand)

    # Reverse variant-to-canonical index.
    variant_to_canonical: dict[str, str] = {}
    for canonical, variants in BRAND_ALIASES.items():
        for variant in variants:
            variant_to_canonical[variant] = canonical

    # Map stored brands to canonical values and collapse duplicates.
    canonicals: set[str] = set()
    brand_expand: dict[str, list[str]] = {}
    for raw in raw_brands:
        canonical = variant_to_canonical.get(raw, raw)
        canonicals.add(canonical)
        brand_expand.setdefault(canonical, []).append(raw)

    # Include the canonical spelling even when absent from stored rows.
    for canonical, variants in BRAND_ALIASES.items():
        if canonical in canonicals:
            existing = set(brand_expand[canonical])
            for variant in variants:
                if variant not in existing:
                    brand_expand[canonical].append(variant)

    return {
        "sub_to_cat": sub_to_cat,
        "brands": sorted(canonicals),
        "brand_expand": brand_expand,
    }


def expand_brands(canonical_brands: list[str]) -> list[str]:
    """Expand canonical brands to stored variants for ``$in`` and ``$nin``."""
    taxonomy = load_taxonomy()
    expand: dict[str, list[str]] = taxonomy["brand_expand"]  # type: ignore[assignment]
    variant_to_canonical: dict[str, str] = {}
    for canonical, variants in BRAND_ALIASES.items():
        for variant in variants:
            variant_to_canonical[variant] = canonical
    for canonical, variants in _EXTRA_BRAND_ALIASES.items():
        for variant in variants:
            variant_to_canonical[variant] = canonical
    out: list[str] = []
    seen: set[str] = set()
    for raw in canonical_brands:
        canonical = variant_to_canonical.get(raw, raw)
        variants = list(expand.get(canonical, [canonical]))
        for extra in BRAND_ALIASES.get(canonical, ()) + _EXTRA_BRAND_ALIASES.get(canonical, ()):
            if extra not in variants:
                variants.append(extra)
        for variant in variants:
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    return out


# ---------------------------------------------------------------------------
# 3. Postfilter vocabulary
# ---------------------------------------------------------------------------

# This stable vocabulary constrains extraction and supports keyword postfiltering. Add a
# term only after confirming that it appears in catalog text.
INGREDIENT_BLOCKLIST: tuple[str, ...] = (
    "酒精", "乙醇", "香精", "色素", "防腐剂",
    "糖", "蔗糖", "果葡糖浆",
    "咖啡因", "咖啡",
    "日系", "韩系",
)


# ---------------------------------------------------------------------------
# 4. Orchestration entry point
# ---------------------------------------------------------------------------

def normalize_query(query: str) -> str:
    return " ".join(query.strip().split())


@lru_cache(maxsize=1024)
def _cached_understand(normalized_query: str) -> ParsedQuery:
    # Delay the import so data-contract consumers do not require the OpenAI package.
    from search.llm_parser import parse_query_with_llm

    return parse_query_with_llm(normalized_query)


def understand_query(query: str) -> ParsedQuery:
    """Public query-understanding entry point for API, agent, and retrieval layers.

    Successful results use an in-process LRU cache. LLM failures keep the raw query for
    vector retrieval and skip inferred positive filters. Failed results are not cached,
    allowing a later retry.
    """
    normalized = normalize_query(query)
    if not normalized:
        return ParsedQuery(original_query=query, needs_clarification=True)
    try:
        parsed = _cached_understand(normalized)
        return _merge_regex_brand_excludes(parsed, query)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "LLM query understanding failed, degrade to vector-only recall: %r", exc
        )
        # Preserve raw vector retrieval plus deterministic explicit exclusions.
        sub_excl, cat_excl = _regex_extract_excludes(query)
        brand_excl = _regex_extract_brand_excludes(query)
        return ParsedQuery(
            original_query=query,
            retrieval_query=normalized,
            brand_exclude=brand_excl,
            sub_category_exclude=sub_excl,
            category_exclude=cat_excl,
            needs_clarification=False,
        )


def clear_cache() -> None:
    """Clear caches after taxonomy or prompt changes."""
    _cached_understand.cache_clear()
    load_taxonomy.cache_clear()


# Common lexical cues for explicit exclusion intent.
_NEGATION_CUES = ("不要", "不想要", "不想", "不含", "不带", "没有", "无", "非", "除了", "排除", "拒绝", "讨厌")

# Colloquial category aliases mapped to official taxonomy values for deterministic
# exclusion fallback. Values must exactly match stored subcategories.
_SUBCAT_SYNONYMS: dict[str, str] = {
    # Food and beverages
    "功能性饮料": "功能饮料",
    "能量饮料": "功能饮料",
    "功能型饮料": "功能饮料",
    "提神饮料": "功能饮料",
    "碳酸": "碳酸饮料",
    "汽水": "碳酸饮料",
    "气泡水": "碳酸饮料",
    "可乐": "碳酸饮料",
    "零食": "坚果/零食",
    "坚果": "坚果/零食",
    "小零食": "坚果/零食",
    "膨化食品": "坚果/零食",
    "速食": "方便食品",
    "泡面": "方便食品",
    "方便面": "方便食品",
    "酸奶": "酸奶",
    "牛奶": "牛奶",
    "纯牛奶": "牛奶",
    "茶": "茶饮",
    "茶饮料": "茶饮",
    # Consumer electronics
    "蓝牙耳机": "真无线耳机",
    "无线耳机": "真无线耳机",
    "耳机": "真无线耳机",
    "手机": "智能手机",
    "平板": "平板电脑",
    "ipad": "平板电脑",
    "笔记本": "笔记本电脑",
    "电脑": "笔记本电脑",
    "笔电": "笔记本电脑",
    # Apparel and sports
    "慢跑鞋": "跑步鞋",
    "运动鞋": "跑步鞋",
    "球鞋": "篮球鞋",
    "登山鞋": "徒步鞋",
    "短袖": "短袖T恤",
    "t恤": "短袖T恤",
    "短裤": "运动短裤",
    "长裤": "运动长裤",
    "卫衣": "卫衣",
    "瑜伽裤": "瑜伽裤",
    "速干衣": "速干T恤",
    "双肩包": "背包",
    "书包": "背包",
    # Beauty and skincare
    "补水面膜": "面膜",
    "贴片面膜": "面膜",
    "面膜": "面膜",
    "精华液": "精华",
    "精华": "精华",
    "面部精华": "精华",
    "爽肤水": "化妆水",
    "化妆水": "化妆水",
    "卸妆水": "卸妆",
    "卸妆油": "卸妆",
    "卸妆膏": "卸妆",
    "洗面奶": "洁面",
    "洁面乳": "洁面",
    "面部防晒": "防晒",
    "防晒霜": "防晒",
    "防晒": "防晒",
    "口红": "唇釉",
    "唇釉": "唇釉",
    "面霜": "面霜",
    "乳液": "面霜",
    "眼霜": "眼霜",
    "粉底": "粉底液",
    "粉底液": "粉底液",
    "散粉": "蜜粉",
    "蜜粉": "蜜粉",
    "眉笔": "眉笔",
}


def _regex_extract_excludes(query: str) -> tuple[list[str], list[str]]:
    """Extract deterministic category exclusions when LLM parsing fails.

    Extraction requires an exclusion cue and accepts only real taxonomy values or mapped
    aliases, preventing unrelated negative phrases from becoming category filters.
    """
    text = query or ""
    if not any(cue in text for cue in _NEGATION_CUES):
        return [], []

    taxonomy = load_taxonomy()
    sub_to_cat = taxonomy.get("sub_to_cat", {}) or {}
    known_cats = {c for c in sub_to_cat.values() if c}

    # Split slash-delimited subcategories so either component can match the official value.
    sub_parts: dict[str, str] = {}
    for sub in sub_to_cat:
        for part in re.split(r"[/、，,]", sub):
            part = part.strip()
            if len(part) >= 2:
                sub_parts.setdefault(part, sub)

    sub_excl: list[str] = []
    cat_excl: list[str] = []

    def _add_sub(official: str) -> None:
        if official in sub_to_cat and official not in sub_excl:
            sub_excl.append(official)

    # Match aliases and official categories in a short window after each exclusion cue.
    for cue in _NEGATION_CUES:
        idx = text.find(cue)
        if idx < 0:
            continue
        window = text[idx + len(cue): idx + len(cue) + 12]
        # 1. Colloquial alias to official subcategory.
        for syn, official in _SUBCAT_SYNONYMS.items():
            if syn in window:
                _add_sub(official)
        # 2. Direct official subcategory match.
        for sub in sub_to_cat:
            if sub in window:
                _add_sub(sub)
        # 3. Component of a slash-delimited official subcategory.
        for part, official in sub_parts.items():
            if part in window:
                _add_sub(official)
        # 4. Parent category match.
        for cat in known_cats:
            if cat in window and cat not in cat_excl:
                cat_excl.append(cat)

    return sub_excl, cat_excl


def _regex_extract_brand_excludes(query: str) -> list[str]:
    """Extract deterministic brand exclusions, including compact expressions."""
    text = query or ""
    if not any(cue in text for cue in _NEGATION_CUES):
        return []

    taxonomy = load_taxonomy()
    brands = list(taxonomy.get("brands", []) or [])
    for canonical, variants in BRAND_ALIASES.items():
        if canonical not in brands:
            brands.append(canonical)
        for variant in variants:
            if variant not in brands:
                brands.append(variant)
    for canonical, variants in _EXTRA_BRAND_ALIASES.items():
        if canonical not in brands:
            brands.append(canonical)
        for variant in variants:
            if variant not in brands:
                brands.append(variant)

    excludes: list[str] = []
    for cue in _NEGATION_CUES:
        start = 0
        while True:
            idx = text.find(cue, start)
            if idx < 0:
                break
            # The existence-question pattern is not an exclusion cue.
            if cue == "没有" and idx > 0 and text[idx - 1] == "有":
                start = idx + len(cue)
                continue
            window = text[idx + len(cue): idx + len(cue) + 18]
            for brand in brands:
                if brand and brand in window and brand not in excludes:
                    excludes.append(brand)
            start = idx + len(cue)
    return excludes


def _drop_conflicting_excludes(
    brand_include: list[str] | None,
    brand_exclude: list[str],
) -> list[str]:
    """Remove exclusions that conflict with included brand variants.

    Variant-aware comparison prevents simultaneous ``$in`` and ``$nin`` filters for the
    same brand. Inclusion wins on conflict.
    """
    if not brand_include or not brand_exclude:
        return list(brand_exclude or [])
    inc_keys = {b.strip().lower().replace(" ", "") for b in expand_brands(brand_include)}
    out: list[str] = []
    for brand in brand_exclude:
        variants = {v.strip().lower().replace(" ", "") for v in expand_brands([brand])}
        if variants & inc_keys:
            continue
        out.append(brand)
    return out


def _merge_regex_brand_excludes(parsed: ParsedQuery, query: str) -> ParsedQuery:
    brand_excludes = list(parsed.brand_exclude or [])
    for brand in _regex_extract_brand_excludes(query):
        if brand not in brand_excludes:
            brand_excludes.append(brand)
    # A brand cannot be both included and excluded.
    brand_excludes = _drop_conflicting_excludes(parsed.brand_include, brand_excludes)
    if brand_excludes == list(parsed.brand_exclude or []):
        return parsed
    return ParsedQuery(
        original_query=parsed.original_query,
        intent=parsed.intent,
        category=parsed.category,
        sub_category=parsed.sub_category,
        category_exclude=parsed.category_exclude,
        sub_category_exclude=parsed.sub_category_exclude,
        max_price=parsed.max_price,
        min_price=parsed.min_price,
        brand_include=parsed.brand_include,
        brand_exclude=brand_excludes,
        negative_ingredients=parsed.negative_ingredients,
        soft_terms=parsed.soft_terms,
        retrieval_query=parsed.retrieval_query,
        needs_clarification=parsed.needs_clarification,
    )


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse a user query into structured retrieval input")
    parser.add_argument("query")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    args = parse_args()
    if args.db != DEFAULT_DB_PATH:
        # Reset taxonomy cache when the CLI overrides the default database.
        load_taxonomy.cache_clear()
        load_taxonomy(args.db)
    parsed = understand_query(args.query)
    print(json.dumps(parsed.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
