from __future__ import annotations

"""Query Understanding 编排入口（LLM 主力版）。

职责边界：
    本模块只做「数据契约 + 编排 + 缓存」，不做任何关键字/正则解析。
    自然语言到 ParsedQuery 的真实工作全部交给 search.llm_parser，
    由当前主 LLM 的 function calling 完成（详见 docs/后端业务技术选型与流程设计.md）。

为什么不再保留规则兜底：
    规则版在 query 覆盖率、否定语义、同义词等维度上全面落后 LLM，
    保留它只会带来「降级到一个同样不准的版本」的虚假安全感。
    LLM 不可用时直接 raise，由上层 API 决定是返回错误还是空结果，
    比悄悄给出错误召回更负责任。

依赖关系：
    llm_parser  ──imports──►  query_understanding   （取 ParsedQuery / load_taxonomy / INGREDIENT_BLOCKLIST）
    api / agent ──imports──►  query_understanding   （只调 understand_query）
"""

import argparse
import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field, fields, replace
from functools import lru_cache
from typing import Collection
from pathlib import Path
from catalog_config import DEFAULT_DB_PATH


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    # A price the shopper gave as approximate ("around $50"); min/max then hold the window.
    price_target: float | None = None
    brand_include: list[str] = field(default_factory=list)
    brand_exclude: list[str] = field(default_factory=list)
    negative_ingredients: list[str] = field(default_factory=list)
    soft_terms: list[str] = field(default_factory=list)
    retrieval_query: str = ""
    needs_clarification: bool = False
    # 用户点名、但商品库里没有的品牌或品类（「三星手机」「扫地机器人」）。
    # 非空时检索直接返回空结果，由话术坦诚说明，而不是拿相近的商品顶替。
    unavailable_terms: list[str] = field(default_factory=list)
    # A device the catalog does not sell whose accessories are searched instead ("iPhone").
    instead_of: str | None = None
    # Rejected ingredients or properties, checked against catalog attributes after recall.
    excluded_attributes: list[str] = field(default_factory=list)
    # Knowledge-graph signals; they only propose candidates, never filter.
    aspects: list[str] = field(default_factory=list)
    use_cases: list[str] = field(default_factory=list)
    compatible_with: list[str] = field(default_factory=list)
    # Limits on measured attributes: {"attribute": "battery_hours", "op": ">=", "value": 20.0}.
    numeric: list[dict[str, object]] = field(default_factory=list)
    # Standalone search words written by the model; empty when rules parsed the turn.
    search_text: str = ""
    # Wanted listing attribute values ("noise_cancelling", "leather", "waterproof").
    # A listing that states one ranks higher; silence is not a miss.
    features: list[str] = field(default_factory=list)
    # Conditions in force that this turn takes back, as the model labelled them; ``None`` when
    # rules parsed the turn, so withdrawal falls back to phrase matching.
    withdrawn: list[str] | None = None

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

    def without(self, cleared: "Collection[str]") -> "ParsedQuery":
        """Forget withdrawn constraints so a later turn cannot inherit them.

        ``cleared`` names groups: budget, brand, measurements (numeric limits), exclusions
        (rejected ingredients and properties), device (compatibility) and features.
        """
        changes: dict[str, object] = {}
        if "budget" in cleared:
            changes.update(min_price=None, max_price=None, price_target=None)
        if "brand" in cleared:
            changes.update(brand_include=[], brand_exclude=[])
        if "measurements" in cleared:
            changes.update(numeric=[])
        if "exclusions" in cleared:
            changes.update(excluded_attributes=[], negative_ingredients=[])
        if "device" in cleared:
            changes.update(compatible_with=[])
        if "features" in cleared:
            changes.update(features=[])
        return replace(self, **changes) if changes else self

    def switches_from(self, base: "ParsedQuery") -> bool:
        """Whether this turn names a different category, which the old conditions don't apply to."""
        return bool(
            (self.sub_category and base.sub_category and self.sub_category != base.sub_category)
            or (self.category and base.category and self.category != base.category)
        )

    def merge_base(self, base: "ParsedQuery") -> "ParsedQuery":
        """Overlay the current parsed turn on the previous structured intent.

        Scalar category and price fields override only when present. A non-empty brand
        inclusion replaces the old brand, while exclusions, negative ingredients, and
        soft preferences accumulate. Retrieval terms are merged with current terms first,
        and inherited context makes clarification unnecessary.
        """
        if self.switches_from(base):
            return replace(self, needs_clarification=False)

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
            unavailable_terms=list(self.unavailable_terms or []),
            excluded_attributes=union(self.excluded_attributes, base.excluded_attributes),
            aspects=union(self.aspects, base.aspects),
            use_cases=union(self.use_cases, base.use_cases),
            # A newly named device replaces the old one: the shopper switched phones.
            compatible_with=replace_or_keep(self.compatible_with, base.compatible_with),
            features=union(self.features, base.features),
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
    "Apple 苹果": ("Apple", "apple", "iphone", "iPhone", "ipad", "iPad", "macbook", "MacBook", "airpods", "AirPods"),
    "阿迪达斯": ("adidas", "Adidas", "三叶草"),
    "露露乐蒙": ("lululemon", "Lululemon"),
    "始祖鸟": ("arcteryx", "Arc'teryx"),
    "萨洛蒙": ("salomon", "SALOMON", "Salomon"),
    "迈乐": ("merrell", "Merrell"),
    "迪卡侬": ("decathlon", "Decathlon"),
    "欧莱雅": ("巴黎欧莱雅",),
    "巴黎欧莱雅": ("欧莱雅", "loreal", "L'Oreal"),
    "资生堂": ("shiseido", "Shiseido"),
    "雅诗兰黛": ("estee lauder", "Estee Lauder"),
    "兰蔻": ("lancome", "Lancome"),
    "科颜氏": ("kiehl's", "Kiehl's", "kiehls"),
    "理肤泉": ("la roche-posay", "La Roche-Posay"),
}


def brand_is_named_in(brand: str, query: str) -> bool:
    """用户是否在这句话里真的提到了这个品牌（含别名）。

    LLM 会凭俗称猜品牌（「红腰子」猜成兰蔻），猜错后品牌硬过滤会把正确商品直接挡掉。
    品牌只有被用户明确说出来才能进硬过滤，其余情况交给向量检索。
    """
    text = (query or "").lower()
    if not text or not brand:
        return False
    candidates = {brand, *BRAND_ALIASES.get(brand, ()), *_EXTRA_BRAND_ALIASES.get(brand, ())}
    for canonical, variants in BRAND_ALIASES.items():
        if brand in variants:
            candidates.update([canonical, *variants, *_EXTRA_BRAND_ALIASES.get(canonical, ())])
    # 「Apple 苹果」这种复合写法，拆开任一段说到都算
    for candidate in list(candidates):
        candidates.update(part for part in candidate.split() if len(part) >= 2)
    return any(candidate.lower() in text for candidate in candidates if candidate)


@lru_cache(maxsize=1)
def load_taxonomy(db_path: Path = DEFAULT_DB_PATH) -> dict[str, object]:
    """Read distinct category, subcategory, and brand values from ``products``.

    The result maps subcategories to parents, exposes canonical brands for the LLM enum,
    and expands canonical brands to stored variants. It is cached once per process.
    """
    if not db_path.exists():
        return {
            "sub_to_cat": {}, "brands": [], "brand_expand": {}, "titles": [], "catalog_text": "", "brand_subs": {},
            "groups": _collections(),
        }

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT category, sub_category, brand FROM products"
        ).fetchall()
        title_rows = conn.execute("SELECT title, sub_category FROM products").fetchall()
        try:
            descriptions = conn.execute("SELECT marketing_description FROM product_descriptions").fetchall()
        except sqlite3.Error:
            descriptions = []
        try:
            group_rows = conn.execute(
                "SELECT catalog_group, label FROM catalog_taxonomy WHERE catalog_group IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            group_rows = []
    groups: dict[str, list[str]] = {}
    for group, label in group_rows:
        groups.setdefault(group, []).append(label)
    groups.update(_collections())

    sub_to_cat: dict[str, str] = {}
    raw_brands: set[str] = set()
    brand_subs: dict[str, set[str]] = {}
    for category, sub_category, brand in rows:
        if sub_category:
            sub_to_cat[sub_category] = category
        if brand:
            raw_brands.add(brand)
            brand_subs.setdefault(brand, set()).add(sub_category)
    catalog_text = " ".join(
        [*(title for title, _ in title_rows), *(text or "" for (text,) in descriptions), *raw_brands]
    ).lower()

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
        # 校验 LLM 解析结果用的事实：商品名、商品文本、品牌实际覆盖的子类目
        "titles": [(title, sub) for title, sub in title_rows],
        "catalog_text": catalog_text,
        "brand_subs": {brand: sorted(subs) for brand, subs in brand_subs.items()},
        "groups": groups,
    }


def _collections() -> dict[str, list[str]]:
    """Named sets of types from the taxonomy file ("Activewear"), which span groups."""
    from store.import_amazon import TAXONOMY_PATH

    try:
        taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: list(spec["members"]) for name, spec in taxonomy.get("collections", {}).items()}


def expand_sub_categories(names: list[str]) -> list[str]:
    """Expand catalog groups to their leaf labels for ``$in`` and ``$nin``."""
    groups: dict[str, list[str]] = load_taxonomy()["groups"]  # type: ignore[assignment]
    return list(dict.fromkeys(label for name in names for label in groups.get(name, [name])))


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


# 并发去重：orchestrator 在 router 还在跑时就预取 query 解析；若 tool 阶段紧接着
# 要同一个结果，等那个在飞的调用而不是再打一次 LLM。
_DEDUPER: "Deduper | None" = None


def _deduper():
    global _DEDUPER
    if _DEDUPER is None:
        from llm.cache import Deduper

        _DEDUPER = Deduper(max_workers=2, name="understand")
    return _DEDUPER


def prefetch_understand(query: str) -> None:
    """后台预取 query 解析（结果落进 LRU）；失败静默，正式调用时会重试。"""
    normalized = normalize_query(query or "")
    if not normalized:
        return
    _deduper().prefetch(("understand", normalized), lambda: _cached_understand(normalized))


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
        parsed = _deduper().run(("understand", normalized), lambda: _cached_understand(normalized))
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
    return replace(parsed, brand_exclude=brand_excludes)


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
