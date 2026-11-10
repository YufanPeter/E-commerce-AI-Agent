"""English query understanding for the Amazon catalog.

Three layers produce one ``ParsedQuery``: a vocabulary generated from the reviewed
taxonomy, deterministic rules that always run, and an optional model pass. Model output
never reaches a filter unverified: labels come from enums, brands must be named by the
user, and prices must appear in the query.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from functools import lru_cache
import json
import logging
import re
import sqlite3
from typing import Any

from catalog_config import AMAZON_CATEGORIES, PROFILE
from i18n import en as english
from llm.client import get_client, get_model_id
from search.llm_parser import _extract_tool_arguments, _coerce_float
from search.query_understanding import ParsedQuery
from store.import_amazon import TAXONOMY_PATH, brand_key
from store.product_store import english_brand_named


logger = logging.getLogger(__name__)

_OUTSIDE = "__outside_catalog__"
_NEGATIVE = re.compile(
    r"\b(?:no|not|exclude|excluding|avoid|without|except|instead of|don'?t want)\s+"
    r"(?:(?:[\w'-]+\s+){0,3}(?:or|nor)\s+)?$",
    re.I,
)
_AMOUNT = r"(\d+(?:,\d{3})*(?:\.\d{1,2})?)"
_RANGE = re.compile(r"\bbetween\s+\$?\s*" + _AMOUNT + r"\s+(?:and|to)\s+\$?\s*" + _AMOUNT, re.I)
_MAX = re.compile(r"\b(?:under|below|up to|at most|less than|no more than|within|budget(?:\s+(?:of|to|is|at|around))?)\s*\$?\s*" + _AMOUNT, re.I)
_MIN = re.compile(r"\b(?:over|above|at least|more than|from)\s*\$?\s*" + _AMOUNT, re.I)
_OTHER_CURRENCY = re.compile(r"[€£¥￥]|\b(?:EUR|GBP|CAD|CNY|RMB)\b", re.I)
_NO_BUDGET = re.compile(r"\b(?:no budget|any price|no price limit|clear (?:the )?budget)\b", re.I)
_ANY_BRAND = re.compile(r"\b(?:any brand|all brands|clear (?:the )?brand filter)\b", re.I)
_BRAND_CUE = re.compile(r"\b(only|by|no|not|exclude|avoid|without|except)\s+", re.I)
_ATTRIBUTES = r"alcohol|fragrance|perfume|parabens?|sulfates?|silicones?|oil|phthalates?|gluten|soy|nuts?|latex|bpa|dyes?"
_ATTRIBUTE_NEGATED = re.compile(
    rf"\b(?:no|without|free of|avoid|non)[- ]+({_ATTRIBUTES})\b|\b({_ATTRIBUTES})[- ]free\b", re.I,
)
_WOMEN = re.compile(r"\b(?:women'?s?|woman'?s?|ladies|lady'?s?|female)\b", re.I)
_MEN = re.compile(r"(?<![a-z])(?:men'?s?|man'?s|gents?|male)\b", re.I)
_LABEL_AUDIENCE = re.compile(r"^(?:Novelty )?(?:Men's|Women's)\s+")


@dataclass(frozen=True)
class CatalogVocabulary:
    categories: tuple[str, ...]
    labels: dict[str, str]
    groups: dict[str, tuple[str, ...]]
    audiences: dict[str, str]
    patterns: dict[str, re.Pattern[str]]
    brands: dict[str, str]
    accessory_labels: frozenset[str] = frozenset()

    @property
    def product_types(self) -> list[str]:
        return sorted({*self.labels, *self.groups})

    def expand(self, names: list[str] | tuple[str, ...]) -> list[str]:
        """Replace each group with its leaf labels, keeping order."""
        return list(dict.fromkeys(label for name in names for label in self.groups.get(name, (name,))))

    def match_types(self, text: str) -> tuple[str | None, list[str], str | None]:
        """Return the wanted type, excluded types, and any out-of-catalog phrase."""
        hits = sorted(
            ((m.start(), m.end(), key) for key, pattern in self.patterns.items() for m in pattern.finditer(text)),
            key=lambda hit: (-(hit[1] - hit[0]), hit[0]),
        )
        selected: list[tuple[int, int, str]] = []
        for start, end, key in hits:
            if not any(start < right and end > left for left, right, _ in selected):
                selected.append((start, end, key))
        wanted, excluded, outside = [], [], None
        for start, end, key in sorted(selected):
            negated = bool(_NEGATIVE.search(text[:start]))
            if key == _OUTSIDE:
                outside = outside or (None if negated else text[start:end])
            elif negated:
                excluded.append(key)
            else:
                wanted.append(key)
        # A device name beside a catalog type ("iPhone 15 screen protector") is compatibility.
        return (wanted[0] if wanted else None), list(dict.fromkeys(excluded)), (None if wanted else outside)

    def canonical_brand(self, name: str) -> str | None:
        return self.brands.get(brand_key(name or ""))


def _compile(terms: list[str]) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(terms) + r")(?![\w-])", re.I)


def load_vocabulary(conn: sqlite3.Connection) -> CatalogVocabulary:
    rows = conn.execute(
        "SELECT label, category, catalog_group, audience, terms_json, product_role FROM catalog_taxonomy ORDER BY label"
    ).fetchall()
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    labels, audiences, members, patterns = {}, {}, {}, {}
    accessories: set[str] = set()
    # Groups go first so an equally long match prefers the broader type.
    for group, terms in taxonomy["groups"].items():
        patterns[group] = _compile(terms)
    for row in rows:
        label, category, group, audience, terms, role = tuple(row)
        labels[label] = category
        if role == "accessory":
            accessories.add(label)
        if audience:
            audiences[label] = audience
        if group:
            members.setdefault(group, []).append(label)
        patterns[label] = _compile(json.loads(terms))
    patterns[_OUTSIDE] = _compile(taxonomy["unavailable_terms"])
    brand_rows = conn.execute(
        """SELECT DISTINCT p.brand FROM products p JOIN amazon_product_metadata a USING (product_id)
           WHERE p.brand <> '' AND a.is_generic_brand = 0"""
    ).fetchall()
    return CatalogVocabulary(
        categories=tuple(AMAZON_CATEGORIES.values()),
        labels=labels,
        groups={group: tuple(values) for group, values in members.items()},
        audiences=audiences,
        patterns=patterns,
        brands={brand_key(row[0]): row[0] for row in brand_rows},
        accessory_labels=frozenset(accessories),
    )


def _amount(match: re.Match[str] | None, group: int = 1) -> float | None:
    return float(match[group].replace(",", "")) if match else None


def _explicit_brands(query: str, vocabulary: CatalogVocabulary) -> tuple[list[str], list[str]]:
    included, excluded = [], []
    names = sorted(vocabulary.brands.values(), key=len, reverse=True)
    for cue in _BRAND_CUE.finditer(query):
        tail = query[cue.end():].casefold()
        brand = next(
            (name for name in names
             if tail.startswith(name.casefold()) and (len(tail) == len(name) or not tail[len(name)].isalnum())),
            None,
        )
        if brand is not None:
            (included if cue[1].lower() in {"only", "by"} else excluded).append(brand)
    return included, excluded


def _brand_before_type(query: str, product_type: str, vocabulary: CatalogVocabulary) -> list[str]:
    """Catch "Sony headphones"; a brand elsewhere in the text stays a ranking signal."""
    match = vocabulary.patterns[product_type].search(query)
    prefix = query[:match.start()].rstrip().lower() if match else ""
    for name in sorted(vocabulary.brands.values(), key=len, reverse=True):
        lowered = name.lower()
        if prefix.endswith(lowered) and english_brand_named(lowered, prefix):
            return [name]
    return []


def _excluded_attributes(query: str) -> list[str]:
    return list(dict.fromkeys((m[1] or m[2]).lower().rstrip("s") for m in _ATTRIBUTE_NEGATED.finditer(query)))


def parse_rules(query: str, vocabulary: CatalogVocabulary) -> ParsedQuery:
    """Deterministic parse; also the complete fallback when no model is available."""
    product_type, excluded_types, outside = vocabulary.match_types(query)
    category = next((name for name in vocabulary.categories if name.casefold() in query.casefold()), None)
    bounds = _RANGE.search(query)
    included, excluded = _explicit_brands(query, vocabulary)
    if not included and product_type:
        included = _brand_before_type(query, product_type, vocabulary)
    unavailable = [outside] if outside else []
    if _OTHER_CURRENCY.search(query):
        unavailable.append("This catalog only supports USD budgets")
    return ParsedQuery(
        original_query=query,
        category=category,
        sub_category=product_type,
        sub_category_exclude=excluded_types,
        brand_include=included,
        brand_exclude=excluded,
        min_price=_amount(bounds) if bounds else _amount(_MIN.search(query)),
        max_price=_amount(bounds, 2) if bounds else _amount(_MAX.search(query)),
        retrieval_query=query,
        unavailable_terms=unavailable,
        excluded_attributes=_excluded_attributes(query),
    )


def _tool_schema(vocabulary: CatalogVocabulary) -> dict[str, Any]:
    types = vocabulary.product_types
    categories = list(vocabulary.categories)
    strings = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "function",
        "function": {
            "name": "extract_search_intent",
            "description": "Extract structured retrieval intent from an English shopping query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": ["string", "null"], "enum": [*categories, None]},
                    "product_type": {"type": ["string", "null"], "enum": [*types, None]},
                    "brand_include": {**strings, "description": "Brands the user explicitly asks for, spelled as they wrote them."},
                    "brand_exclude": {**strings, "description": "Brands the user explicitly rejects."},
                    "category_exclude": {"type": "array", "items": {"type": "string", "enum": categories}},
                    "product_type_exclude": {"type": "array", "items": {"type": "string", "enum": types}},
                    "max_price": {"type": ["number", "null"], "description": "Upper budget in USD."},
                    "min_price": {"type": ["number", "null"], "description": "Lower budget in USD."},
                    "excluded_attributes": {**strings, "description": "Ingredients or properties the user rejects, as single nouns such as alcohol."},
                    "soft_terms": {**strings, "description": "Wanted properties that are preferences, not filters."},
                    "unavailable_terms": {**strings, "description": "The user's own words for a product type absent from the enum."},
                    "needs_clarification": {"type": "boolean"},
                },
                "required": [
                    "category", "product_type", "brand_include", "brand_exclude", "category_exclude",
                    "product_type_exclude", "max_price", "min_price", "excluded_attributes", "soft_terms",
                    "unavailable_terms", "needs_clarification",
                ],
                "additionalProperties": False,
            },
        },
    }


def _named_brands(values: Any, query: str, vocabulary: CatalogVocabulary) -> list[str]:
    """Keep only catalog brands the user actually said."""
    out: list[str] = []
    for value in values or []:
        canonical = vocabulary.canonical_brand(str(value))
        named = english_brand_named(str(value).lower(), query.lower()) or (
            canonical is not None and english_brand_named(canonical.lower(), query.lower())
        )
        if canonical and named and canonical not in out:
            out.append(canonical)
    return out


def _stated_price(value: Any, query: str) -> float | None:
    """Reject prices invented from model numbers; a query without digits spells the amount out."""
    number = _coerce_float(value)
    if number is None or number < 0:
        return None
    digits = {float(raw.replace(",", "")) for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", query)}
    return number if not digits or number in digits else None


def _rejected_terms(values: Any, query: str) -> list[str]:
    """A rejected attribute must be literally negated by the user."""
    out: list[str] = []
    for raw in values or []:
        term = str(raw).strip().lower().rstrip("s")
        if not term:
            continue
        negated = re.search(
            rf"\b(?:no|without|free of|avoid|non|not|exclude|hate|allergic to)\b[^.,;]{{0,24}}\b{re.escape(term)}s?\b"
            rf"|\b{re.escape(term)}s?[- ]free\b",
            query, re.I,
        )
        if negated and term not in out:
            out.append(term)
    return out


def _from_model(query: str, args: dict[str, Any], vocabulary: CatalogVocabulary) -> ParsedQuery:
    types = set(vocabulary.product_types)
    categories = set(vocabulary.categories)
    product_type = args.get("product_type") if args.get("product_type") in types else None
    category = args.get("category") if args.get("category") in categories and not product_type else None
    excluded_types = [t for t in args.get("product_type_exclude") or [] if t in types and t != product_type]
    unavailable = [
        str(term).strip() for term in args.get("unavailable_terms") or []
        if str(term).strip() and str(term).strip().lower() in query.lower() and str(term).strip() not in types
    ]
    return ParsedQuery(
        original_query=query,
        category=category,
        sub_category=product_type,
        category_exclude=[c for c in args.get("category_exclude") or [] if c in categories],
        sub_category_exclude=list(dict.fromkeys(excluded_types)),
        brand_include=_named_brands(args.get("brand_include"), query, vocabulary),
        brand_exclude=_named_brands(args.get("brand_exclude"), query, vocabulary),
        min_price=_stated_price(args.get("min_price"), query),
        max_price=_stated_price(args.get("max_price"), query),
        soft_terms=[str(term) for term in args.get("soft_terms") or []],
        retrieval_query=query,
        needs_clarification=bool(args.get("needs_clarification")),
        unavailable_terms=unavailable if not product_type else [],
        excluded_attributes=_rejected_terms(args.get("excluded_attributes"), query),
    )


def _reconcile(model: ParsedQuery, rules: ParsedQuery) -> ParsedQuery:
    """Trust the model for meaning, and rules for anything it left out."""
    product_type = model.sub_category or rules.sub_category
    return replace(
        model,
        sub_category=product_type,
        category=None if product_type else (model.category or rules.category),
        sub_category_exclude=[t for t in dict.fromkeys(model.sub_category_exclude + rules.sub_category_exclude) if t != product_type],
        brand_include=model.brand_include or rules.brand_include,
        brand_exclude=list(dict.fromkeys(model.brand_exclude + rules.brand_exclude)),
        min_price=model.min_price if model.min_price is not None else rules.min_price,
        max_price=model.max_price if model.max_price is not None else rules.max_price,
        excluded_attributes=list(dict.fromkeys(model.excluded_attributes + rules.excluded_attributes)),
        unavailable_terms=list(dict.fromkeys(rules.unavailable_terms + model.unavailable_terms)),
        needs_clarification=model.needs_clarification and not (product_type or model.category or rules.category),
    )


def _audience_gap(parsed: ParsedQuery, vocabulary: CatalogVocabulary) -> ParsedQuery:
    """Say so when the only matching leaf serves the other audience."""
    audience = vocabulary.audiences.get(parsed.sub_category or "")
    query = parsed.original_query
    wanted = "women" if _WOMEN.search(query) else "men" if _MEN.search(query) else None
    if audience is None or wanted is None or wanted == audience:
        return parsed
    term = f"{wanted.title()}'s {_LABEL_AUDIENCE.sub('', parsed.sub_category or '')}"
    return replace(parsed, unavailable_terms=[*parsed.unavailable_terms, term])


@lru_cache(maxsize=1024)
def _call_model(query: str, vocabulary_id: int, timeout: float) -> str:
    vocabulary = _VOCABULARIES[vocabulary_id]
    response = get_client().chat.completions.create(
        model=get_model_id(role="fast"),
        messages=[{"role": "system", "content": english.PARSER_SYSTEM}, {"role": "user", "content": query}],
        tools=[_tool_schema(vocabulary)],
        tool_choice={"type": "function", "function": {"name": "extract_search_intent"}},
        temperature=0.1,
        timeout=timeout,
    )
    return json.dumps(_extract_tool_arguments(response))


_VOCABULARIES: dict[int, CatalogVocabulary] = {}


def understand(query: str, vocabulary: CatalogVocabulary, timeout: float = 6.0) -> ParsedQuery:
    """Parse one turn. A model failure degrades to rules instead of failing the search."""
    rules = parse_rules(query, vocabulary)
    parsed = rules
    if PROFILE.capabilities.llm_query_parsing:
        _VOCABULARIES[id(vocabulary)] = vocabulary
        try:
            arguments = json.loads(_call_model(" ".join(query.split()), id(vocabulary), timeout))
            parsed = _reconcile(_from_model(query, arguments, vocabulary), rules)
        except Exception as exc:  # noqa: BLE001 - every model failure has the same rule fallback
            logger.warning("Catalog query parsing fell back to rules: %r", exc)
    return _audience_gap(parsed, vocabulary)


def merge_turn(current: ParsedQuery, base: ParsedQuery | None, profile: dict[str, Any]) -> ParsedQuery:
    """Overlay this turn on the previous search and the saved shopper profile."""
    query = current.original_query
    product_type, category, excluded_types = current.sub_category, current.category, current.sub_category_exclude
    switch = bool(base and ((product_type and product_type != base.sub_category) or (category and category != base.category)))
    if base and not switch:
        product_type = product_type or base.sub_category
        category = category or base.category
        excluded_types = list(dict.fromkeys(base.sub_category_exclude + excluded_types))
    minimum, maximum = current.min_price, current.max_price
    if not _NO_BUDGET.search(query):
        minimum = minimum if minimum is not None else (base.min_price if base else profile.get("budget_min"))
        maximum = maximum if maximum is not None else (base.max_price if base else profile.get("budget_max"))
    included, excluded = list(current.brand_include), list(current.brand_exclude)
    if not _ANY_BRAND.search(query):
        denied = {brand.casefold() for brand in excluded}
        if not included:
            included = [brand for brand in (base.brand_include if base else []) if brand.casefold() not in denied]
        excluded += list(base.brand_exclude if base else profile.get("brand_exclude") or [])
    allowed = {brand.casefold() for brand in included}
    return replace(
        current,
        category=category,
        sub_category=product_type,
        sub_category_exclude=excluded_types,
        brand_include=included,
        brand_exclude=[brand for brand in dict.fromkeys(excluded) if brand.casefold() not in allowed],
        min_price=minimum,
        max_price=maximum,
        retrieval_query=f"{query} {base.retrieval_query}" if base and not switch else query,
        needs_clarification=current.needs_clarification and not (
            product_type or category or included or minimum is not None or maximum is not None
        ),
        excluded_attributes=list(dict.fromkeys((base.excluded_attributes if base and not switch else []) + current.excluded_attributes)),
    )


_PRICE_PHRASES = (_RANGE, _MAX, _MIN)
_NEGATED_PHRASE = re.compile(r"\b(?:no|not|without|exclude|excluding|avoid|except|instead of|don'?t want)\b[^,.;]*", re.I)


def semantic_query(parsed: ParsedQuery) -> str:
    """Text for embedding and reranking: what is wanted, without budgets or rejections."""
    text = parsed.retrieval_query or parsed.original_query
    for pattern in _PRICE_PHRASES:
        text = pattern.sub(" ", text)
    text = _NEGATED_PHRASE.sub(" ", text)
    words = list(dict.fromkeys(re.sub(r"[,.;]+", " ", text).split()))
    if parsed.sub_category and parsed.sub_category.lower() not in " ".join(words).lower():
        words.insert(0, parsed.sub_category)
    return " ".join(words)


def open_vocabulary(store: Any) -> CatalogVocabulary:
    with closing(store.connect()) as conn:
        return load_vocabulary(conn)
