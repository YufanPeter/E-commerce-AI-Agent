"""English query understanding for the Amazon catalog.

Three layers produce one ``ParsedQuery``: a vocabulary generated from the reviewed
taxonomy, deterministic rules that always run, and an optional model pass. Model output
never reaches a filter unverified: labels come from enums, brands must be named by the
user, and prices must appear in the query.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field, replace
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
from store.attribute_rules import DESCRIPTION, TITLE, _DEVICES, extract_multi, wanted_values
from store.import_amazon import TAXONOMY_PATH, brand_key
from store.numeric_attributes import describe, numeric_attributes, rule_constraints, stated_constraint, unit_pattern
from store.product_store import english_brand_named


logger = logging.getLogger(__name__)

_OUTSIDE = "__outside_catalog__"
_NEGATIVE = re.compile(
    r"\b(?:no|not|exclude|excluding|avoid|without|except|instead of|don'?t want)\s+"
    r"(?:(?:[\w'-]+\s+){0,3}(?:or|nor)\s+)?$",
    re.I,
)
# A number followed by a unit measures the product, not the budget ("at least 20 hours", "over 100W").
_NOT_A_PRICE = r"(?![.,]?\d)(?!\s*-?\s*" + unit_pattern() + ")"
_AMOUNT = r"(\d+(?:,\d{3})*(?:\.\d{1,2})?)" + _NOT_A_PRICE
_DOLLARS = r"\s*(?:dollars?|bucks|usd)\b"
_RANGE = re.compile(r"\bbetween\s+\$?\s*" + _AMOUNT + r"\s+(?:and|to)\s+\$?\s*" + _AMOUNT, re.I)
# "$40-60", "$40 to $60", "40-60 dollars". A currency mark is required: "20-30 hours" is no budget.
_DASH_RANGE = re.compile(
    r"\$\s*" + _AMOUNT + r"\s*(?:-|–|to)\s*\$?\s*" + _AMOUNT
    + r"|(?<![\w.$])" + _AMOUNT + r"\s*(?:-|–|to)\s*" + _AMOUNT + _DOLLARS,
    re.I,
)
_MAX = re.compile(r"\b(?:under|below|up to|at most|less than|no more than|within|budget(?:\s+(?:of|to|is|at))?)\s*\$?\s*" + _AMOUNT, re.I)
_MIN = re.compile(r"\b(?:over|above|at least|more than|from)\s*\$?\s*" + _AMOUNT, re.I)
_APPROX = r"(?:around|about|approximately|approx\.?|roughly|close to)"
# "around $50", "about 50 bucks", "budget around 50", "~$50", "$50-ish".
_AROUND = re.compile(
    r"\b" + _APPROX + r"\s*\$\s*" + _AMOUNT
    + r"|\b" + _APPROX + r"\s*" + _AMOUNT + _DOLLARS
    + r"|\bbudget\s+(?:of\s+|is\s+)?" + _APPROX + r"\s*\$?\s*" + _AMOUNT
    + r"|~\s*\$?\s*" + _AMOUNT
    + r"|\$\s*" + _AMOUNT + r"\s*-?\s*ish\b",
    re.I,
)
# A price with no cue ("$50 moisturizer", "for 50 bucks") is a target, not a ceiling.
_BARE_PRICE = re.compile(r"\$\s*" + _AMOUNT + r"|(?<![\w.$])" + _AMOUNT + _DOLLARS, re.I)
_AROUND_TOLERANCE = 0.25
_PRICE_LIKE_NUMBER = re.compile(r"(?<![\d.,])" + _AMOUNT, re.I)
_OTHER_CURRENCY = re.compile(r"[€£¥￥]|\b(?:EUR|GBP|CAD|CNY|RMB)\b", re.I)
_BRAND_CUE = re.compile(r"\b(only|by|no|not|exclude|avoid|without|except)\s+", re.I)
_ATTRIBUTES = r"alcohol|fragrance|perfume|parabens?|sulfates?|silicones?|oil|phthalates?|gluten|soy|nuts?|latex|bpa|dyes?"
_ATTRIBUTE_NEGATED = re.compile(
    rf"\b(?:no|without|free of|avoid|non)[- ]+({_ATTRIBUTES})\b|\b({_ATTRIBUTES})[- ]free\b", re.I,
)
_WOMEN = re.compile(r"\b(?:women'?s?|woman'?s?|ladies|lady'?s?|female)\b", re.I)
_MEN = re.compile(r"(?<![a-z])(?:men'?s?|man'?s|gents?|male)\b", re.I)
_LABEL_AUDIENCE = re.compile(r"^(?:Novelty )?(?:Men's|Women's)\s+")

ONTOLOGY_PATH = TAXONOMY_PATH.with_name("amazon_graph_ontology.json")


@dataclass(frozen=True)
class CatalogVocabulary:
    categories: tuple[str, ...]
    labels: dict[str, str]
    groups: dict[str, tuple[str, ...]]
    audiences: dict[str, str]
    patterns: dict[str, re.Pattern[str]]
    brands: dict[str, str]
    accessory_labels: frozenset[str] = frozenset()
    aspects: tuple[str, ...] = ()
    use_cases: tuple[str, ...] = ()
    # Collections that stand in for a device the catalog does not sell ("iPhone" -> its accessories).
    stand_ins: dict[str, re.Pattern[str]] = field(default_factory=dict)

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


def _ontology_values(kind: str) -> tuple[str, ...]:
    ontology = json.loads(ONTOLOGY_PATH.read_text(encoding="utf-8"))
    return tuple(sorted({value for category in ontology["categories"].values() for value in category[kind]}))


def _named_values(query: str, values: tuple[str, ...]) -> list[str]:
    text = query.casefold()
    return [value for value in values if re.search(rf"\b{re.escape(value.replace('_', ' '))}\b", text)]


def _devices_named(text: str) -> list[str]:
    return [str(item["value"]) for item in extract_multi({TITLE: text, DESCRIPTION: ""}, _DEVICES)]


def load_vocabulary(conn: sqlite3.Connection) -> CatalogVocabulary:
    rows = conn.execute(
        "SELECT label, category, catalog_group, audience, terms_json, product_role FROM catalog_taxonomy ORDER BY label"
    ).fetchall()
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    labels, audiences, members, patterns = {}, {}, {}, {}
    accessories: set[str] = set()
    collections = taxonomy.get("collections", {})
    # Collections and groups go first so an equally long match prefers the broader type.
    for name, spec in collections.items():
        patterns[name] = _compile(spec["terms"])
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
    # A collection spans types across groups ("sports clothing" is shorts, socks and running
    # shoes), so a broad request maps to one name the model can pick instead of to nothing.
    stand_ins: dict[str, re.Pattern[str]] = {}
    for name, spec in collections.items():
        present = [label for label in spec["members"] if label in labels]
        if not present:
            patterns.pop(name, None)
            continue
        members[name] = present
        if spec.get("stands_in_for"):
            stand_ins[name] = _compile(spec["stands_in_for"])
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
        aspects=_ontology_values("aspect"),
        use_cases=_ontology_values("use_case"),
        stand_ins=stand_ins,
    )


def _amount(match: re.Match[str] | None, group: int = 1) -> float | None:
    return float(match[group].replace(",", "")) if match else None


def _amounts(match: re.Match[str]) -> list[float]:
    """The numbers an alternation matched, whichever branch it took."""
    return [float(value.replace(",", "")) for value in match.groups() if value is not None]


def price_window(target: float) -> tuple[float, float]:
    """"Around $50" allows a quarter either way, $37.50 to $62.50, instead of a hard $50 cap."""
    return round(target * (1 - _AROUND_TOLERANCE), 2), round(target * (1 + _AROUND_TOLERANCE), 2)


def _rule_prices(query: str) -> tuple[float | None, float | None, float | None]:
    """(min, max, target). Ranges win, then stated limits, then an approximate or bare price."""
    for pattern in (_RANGE, _DASH_RANGE):
        match = pattern.search(query)
        if match:
            low, high = sorted(_amounts(match)[:2])
            return low, high, None
    low, high = _amount(_MIN.search(query)), _amount(_MAX.search(query))
    if low is not None or high is not None:
        return low, high, None
    match = _AROUND.search(query) or _BARE_PRICE.search(query)
    if match is None:
        return None, None, None
    target = _amounts(match)[0]
    return (*price_window(target), target)


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
    min_price, max_price, price_target = _rule_prices(query)
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
        min_price=min_price,
        max_price=max_price,
        price_target=price_target,
        retrieval_query=query,
        unavailable_terms=unavailable,
        excluded_attributes=_excluded_attributes(query),
        aspects=_named_values(query, vocabulary.aspects),
        use_cases=_named_values(query, vocabulary.use_cases),
        compatible_with=_devices_named(query),
        numeric=rule_constraints(query),
        features=wanted_values(query),
    )


def _tool_schema(vocabulary: CatalogVocabulary) -> dict[str, Any]:
    types = vocabulary.product_types
    categories = list(vocabulary.categories)
    strings = {"type": "array", "items": {"type": "string"}}
    measured = numeric_attributes()
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
                    "price_target": {"type": ["number", "null"], "description": english.PARSER_PRICE_TARGET_FIELD},
                    "excluded_attributes": {**strings, "description": "Ingredients or properties the user rejects, as single nouns such as alcohol."},
                    "soft_terms": {**strings, "description": "Wanted properties that are preferences, not filters."},
                    "unavailable_terms": {**strings, "description": "The user's own words for a product type absent from the enum."},
                    "aspects": {"type": "array", "items": {"type": "string", "enum": list(vocabulary.aspects)}},
                    "use_cases": {"type": "array", "items": {"type": "string", "enum": list(vocabulary.use_cases)}},
                    "compatible_with": {**strings, "description": "Devices the product must work with, as the shopper wrote them."},
                    "needs_clarification": {"type": "boolean"},
                    "search_text": {"type": "string", "description": english.PARSER_SEARCH_TEXT_FIELD},
                    "withdrawn": {**strings, "description": english.PARSER_WITHDRAWN_FIELD},
                    "numeric_constraints": {
                        "type": "array",
                        "description": "Limits on measured attributes, converted to the attribute's unit. "
                        + "; ".join(f"{a.name}: {a.label} in {a.unit}" for a in measured.values()),
                        "items": {
                            "type": "object",
                            "properties": {
                                "attribute": {"type": "string", "enum": list(measured)},
                                "op": {"type": "string", "enum": [">=", "<="]},
                                "value": {"type": "number"},
                            },
                            "required": ["attribute", "op", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "category", "product_type", "brand_include", "brand_exclude", "category_exclude",
                    "product_type_exclude", "max_price", "min_price", "price_target", "excluded_attributes", "soft_terms",
                    "unavailable_terms", "needs_clarification", "aspects", "use_cases", "compatible_with",
                    "search_text", "numeric_constraints", "withdrawn",
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
    """Reject prices invented from model numbers or measurements; a query without digits spells the amount out."""
    number = _coerce_float(value)
    if number is None or number < 0:
        return None
    if not re.search(r"\d", query):
        return number
    amounts = {float(match[1].replace(",", "")) for match in _PRICE_LIKE_NUMBER.finditer(query)}
    return number if number in amounts else None


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
    excluded_attributes = _rejected_terms(args.get("excluded_attributes"), query)
    brand_exclude = _named_brands(args.get("brand_exclude"), query, vocabulary)
    min_price, max_price = _stated_price(args.get("min_price"), query), _stated_price(args.get("max_price"), query)
    price_target = _stated_price(args.get("price_target"), query)
    # A target also copied into a bound ("around $50" as max 50) is still approximate.
    if price_target is not None and {min_price, max_price} <= {None, price_target}:
        min_price, max_price = price_window(price_target)
    else:
        price_target = None
    return ParsedQuery(
        original_query=query,
        category=category,
        sub_category=product_type,
        category_exclude=[c for c in args.get("category_exclude") or [] if c in categories],
        sub_category_exclude=list(dict.fromkeys(excluded_types)),
        brand_include=_named_brands(args.get("brand_include"), query, vocabulary),
        brand_exclude=brand_exclude,
        min_price=min_price,
        max_price=max_price,
        price_target=price_target,
        soft_terms=[str(term) for term in args.get("soft_terms") or []],
        retrieval_query=query,
        needs_clarification=bool(args.get("needs_clarification")),
        unavailable_terms=unavailable if not product_type else [],
        excluded_attributes=excluded_attributes,
        aspects=[value for value in args.get("aspects") or [] if value in vocabulary.aspects],
        use_cases=[value for value in args.get("use_cases") or [] if value in vocabulary.use_cases],
        compatible_with=[device for value in args.get("compatible_with") or [] for device in _devices_named(str(value))],
        numeric=[c for c in (stated_constraint(raw, query) for raw in args.get("numeric_constraints") or []) if c],
        search_text=_without_rejections(str(args.get("search_text") or ""), [*excluded_attributes, *brand_exclude]),
        # Wanted properties are what the model read as wanted, normalized by the listing rules.
        # The raw message is not scanned: "I don't need it to be waterproof" names waterproof
        # but does not want it, and only the model can tell. Aspects are wanted qualities too:
        # the model files "noise cancelling" there as noise_cancellation rather than in soft_terms,
        # and its search words keep the ones folded into the product ("bluetooth speaker").
        features=[
            value for value in wanted_values(" ; ".join(
                [str(term) for term in args.get("soft_terms") or []]
                + [str(aspect).replace("_", " ") for aspect in args.get("aspects") or [] if aspect in vocabulary.aspects]
                + [str(args.get("search_text") or "")]
            ))
            if value not in excluded_attributes
        ],
        withdrawn=[str(label).strip() for label in args.get("withdrawn") or [] if str(label).strip()],
    )


def _without_rejections(text: str, rejected: list[str]) -> str:
    """Search words must not name what the shopper rejected; similarity would pull it back in."""
    for term in rejected:
        text = re.sub(
            rf"\b(?:(?:without|no|not|non|avoid|free of)[- ]+)?{re.escape(term)}s?(?:[- ]free)?\b", " ", text, flags=re.I,
        )
    return " ".join(text.split())


def _audience_gap(parsed: ParsedQuery, vocabulary: CatalogVocabulary) -> ParsedQuery:
    """Say so when the only matching leaf serves the other audience."""
    audience = vocabulary.audiences.get(parsed.sub_category or "")
    query = parsed.original_query
    wanted = "women" if _WOMEN.search(query) else "men" if _MEN.search(query) else None
    if audience is None or wanted is None or wanted == audience:
        return parsed
    term = f"{wanted.title()}'s {_LABEL_AUDIENCE.sub('', parsed.sub_category or '')}"
    return replace(parsed, unavailable_terms=[*parsed.unavailable_terms, term])


def _accessories_instead(parsed: ParsedQuery, vocabulary: CatalogVocabulary) -> ParsedQuery:
    """A device the catalog does not sell becomes a search for its accessories.

    "I need an iPhone" finds no phones, but cases, chargers and screen protectors for it are
    there; an empty answer would hide them. ``instead_of`` keeps the device so the reply says
    the device itself is not sold.
    """
    if parsed.sub_category:
        return parsed
    for term in parsed.unavailable_terms:
        for name, pattern in vocabulary.stand_ins.items():
            if pattern.search(term):
                return replace(
                    parsed,
                    sub_category=name,
                    category=None,
                    unavailable_terms=[other for other in parsed.unavailable_terms if other != term],
                    instead_of=term,
                    compatible_with=list(parsed.compatible_with) or _devices_named(term),
                )
    return parsed


@lru_cache(maxsize=1024)
def _call_model(query: str, vocabulary_id: int, timeout: float, previous: str = "") -> str:
    vocabulary = _VOCABULARIES[vocabulary_id]
    message = f"Previous search: {previous}\nMessage: {query}" if previous else query
    response = get_client().chat.completions.create(
        model=get_model_id(role="fast"),
        messages=[{"role": "system", "content": english.PARSER_SYSTEM}, {"role": "user", "content": message}],
        tools=[_tool_schema(vocabulary)],
        tool_choice={"type": "function", "function": {"name": "extract_search_intent"}},
        temperature=0.1,
        timeout=timeout,
    )
    return json.dumps(_extract_tool_arguments(response))


_VOCABULARIES: dict[int, CatalogVocabulary] = {}


def understand(
    query: str, vocabulary: CatalogVocabulary, timeout: float = 6.0, base: ParsedQuery | None = None,
) -> ParsedQuery:
    """Parse one turn with the model; rules run only when it is off or fails.

    ``base`` is the previous search; the model sees its words so ``search_text`` stands alone.
    Model output is checked against the query and catalog, never patched with rule output.
    """
    parsed = None
    if PROFILE.capabilities.llm_query_parsing:
        _VOCABULARIES[id(vocabulary)] = vocabulary
        previous = semantic_query(base) if base is not None else ""
        labels = list(condition_labels(base)) if base is not None else []
        if labels:
            # The model can only take back what it is shown, so the carried conditions go along.
            previous += "\nConditions in force: " + "; ".join(labels)
        try:
            arguments = json.loads(_call_model(" ".join(query.split()), id(vocabulary), timeout, previous))
            parsed = _from_model(query, arguments, vocabulary)
        except Exception as exc:  # noqa: BLE001 - every model failure has the same rule fallback
            logger.warning("Catalog query parsing fell back to rules: %r", exc)
    return _accessories_instead(_audience_gap(parsed or parse_rules(query, vocabulary), vocabulary), vocabulary)


def merge_turn(current: ParsedQuery, base: ParsedQuery | None, profile: dict[str, Any]) -> ParsedQuery:
    """Overlay this turn on the previous search and the saved shopper profile."""
    query = current.original_query
    product_type, category, excluded_types = current.sub_category, current.category, current.sub_category_exclude
    switched = bool(base and ((product_type and product_type != base.sub_category) or (category and category != base.category)))
    carried = None if switched else base  # Conditions from another category do not apply here.
    if carried:
        product_type = product_type or carried.sub_category
        category = category or carried.category
        excluded_types = list(dict.fromkeys(carried.sub_category_exclude + excluded_types))
    # Withdrawn conditions ("any brand is fine") are already gone from ``base``: the router reads
    # them and the refine tool clears them before searching.
    # A saved budget is a ranking signal (``_apply_profile_boost``), never a filter: a usual
    # skincare budget must not hide every pair of headphones without telling the shopper.
    states_price = current.min_price is not None or current.max_price is not None
    # A window around an earlier target is one condition: "cheaper, under $40" after
    # "around $50" replaces it whole instead of keeping its $37.50 floor.
    priced = None if states_price and carried and carried.price_target is not None else carried
    minimum, maximum = current.min_price, current.max_price
    minimum = minimum if minimum is not None else (priced.min_price if priced else None)
    maximum = maximum if maximum is not None else (priced.max_price if priced else None)
    target = current.price_target if states_price else (carried.price_target if carried else None)
    # A new bound that contradicts an inherited one replaces it ("over $100", then "cheaper, under $60").
    if current.max_price is not None and current.min_price is None and minimum is not None and minimum > maximum:
        minimum = None
    if current.min_price is not None and current.max_price is None and maximum is not None and maximum < minimum:
        maximum = None
    # The model names the conditions it read as taken back; phrase matching only stands in when
    # rules parsed the turn, since no list of phrasings covers "I'm ok with alcohol" and the rest.
    model_read = current.withdrawn is not None
    taken_back = _withdrawn_keys(current.withdrawn or [], carried)

    def dropped(key: tuple[str, str], terms: list[str], rejection: bool = False) -> bool:
        return key in taken_back if model_read else released(query, terms, rejection=rejection)

    stated = {(c["attribute"], c["op"]) for c in current.numeric}
    numeric = [
        c for c in (carried.numeric if carried else [])
        if (c["attribute"], c["op"]) not in stated
        and not _contradicted(c, current.numeric)
        and not dropped(("numeric", describe(c)), _measure_terms(str(c["attribute"])))
    ] + current.numeric
    # "Alcohol is fine" withdraws one rejection; the others stay.
    kept_exclusions = [
        term for term in (carried.excluded_attributes if carried else [])
        if term not in current.excluded_attributes and not dropped(("exclusion", term), [term], rejection=True)
    ]
    kept_features = [
        value for value in (carried.features if carried else [])
        if not dropped(("feature", value), [value])
    ]
    # Model features are what it read as wanted, though its search words may still carry one it
    # marked as taken back; rule features may name a withdrawal.
    if model_read:
        new_features = [v for v in current.features if ("feature", v) not in taken_back]
    else:
        new_features = [v for v in current.features if not released(query, [v])]
    # Naming another device means the shopper switched devices, not that it must fit both.
    devices = list(current.compatible_with) or list(carried.compatible_with if carried else [])
    included, excluded = list(current.brand_include), list(current.brand_exclude)
    denied = {brand.casefold() for brand in excluded}
    if not included:
        included = [brand for brand in (carried.brand_include if carried else []) if brand.casefold() not in denied]
    excluded += list(carried.brand_exclude if carried else profile.get("brand_exclude") or [])
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
        price_target=target,
        retrieval_query=current.search_text or _carried_query(query, carried),
        numeric=numeric,
        needs_clarification=current.needs_clarification and not (
            product_type or category or included or minimum is not None or maximum is not None
        ),
        excluded_attributes=list(dict.fromkeys(kept_exclusions + current.excluded_attributes)),
        aspects=list(dict.fromkeys((carried.aspects if carried else []) + current.aspects)),
        use_cases=list(dict.fromkeys((carried.use_cases if carried else []) + current.use_cases)),
        compatible_with=devices,
        features=list(dict.fromkeys(kept_features + new_features)),
        withdrawn=None,  # Applied here; the merged search carries no pending withdrawal.
    )


def condition_labels(base: ParsedQuery) -> dict[str, tuple[str, str]]:
    """Labels the model sees for carried conditions it may take back, each with its merge key."""
    labels: dict[str, tuple[str, str]] = {}
    for value in base.features:
        labels[value.replace("_", " ")] = ("feature", value)
    for term in base.excluded_attributes:
        labels[f"no {term}"] = ("exclusion", term)
    for constraint in base.numeric:
        labels[describe(constraint)] = ("numeric", describe(constraint))
    return labels


def _withdrawn_keys(withdrawn: list[str], carried: ParsedQuery | None) -> set[tuple[str, str]]:
    """Merge keys for the labels the model returned; a label naming nothing carried is ignored."""
    if not withdrawn or carried is None:
        return set()
    by_label = {label.casefold(): key for label, key in condition_labels(carried).items()}
    # "alcohol" for "no alcohol" and "noise_cancelling" for "noise cancelling" mean the same condition.
    by_label.update({key[1].casefold(): key for key in by_label.values()})
    return {by_label[label.casefold()] for label in withdrawn if label.casefold() in by_label}


def _contradicted(earlier: dict[str, Any], stated: list[dict[str, Any]]) -> bool:
    """A new opposite limit that leaves no overlap replaces the earlier one (">= 20h", then "<= 10h")."""
    for new in stated:
        if new["attribute"] != earlier["attribute"] or new["op"] == earlier["op"]:
            continue
        low, high = (earlier["value"], new["value"]) if earlier["op"] == ">=" else (new["value"], earlier["value"])
        if float(low) > float(high):
            return True
    return False


def _measure_terms(attribute: str) -> list[str]:
    """Words a shopper uses for a measured attribute: "battery", "battery life", "playtime"."""
    spec = numeric_attributes().get(attribute)
    words = [attribute.split("_")[0]]
    if spec is not None:
        words += [part.strip() for part in spec.label.split(" or ") if part.strip()]
    return list(dict.fromkeys(words))


# Accepting something once rejected ("alcohol is fine") versus dropping something once
# required ("doesn't need to be waterproof"). "I don't need fragrance" rejects, so the
# need-phrases never release a rejection.
_ACCEPTS = (
    r"\b{t}\b[^.,;!?]{{0,24}}?\b(?:is|are|'s|would be)?\s*(?:fine|ok|okay|alright|acceptable)\b",
    r"\b(?:don'?t|do not|doesn'?t|does not)\s+(?:mind|care about)\s+(?:the\s+|any\s+)?{t}\b",
    r"\b{t}\b[^.,;!?]{{0,24}}?\b(?:doesn'?t|does not|won'?t|don'?t|do not)\s+matter\b",
)
_DROPS = (
    r"\b{t}\b[^.,;!?]{{0,24}}?\b(?:doesn'?t|does not|won'?t|don'?t|do not)\s+matter\b",
    r"\b(?:don'?t|do not|doesn'?t|does not)\s+(?:need|have)\s+to\s+(?:be|have)\s+(?:a\s+|an\s+)?{t}\b",
    r"\b(?:don'?t|do not|doesn'?t|does not)\s+(?:need|care about)\s+(?:a\s+|an\s+|the\s+|any\s+)?{t}\b",
    r"\b(?:forget|drop|ignore|never mind|skip)\s+(?:about\s+)?(?:the\s+)?{t}\b",
    r"\b{t}\b[^.,;!?]{{0,24}}?(?:\bnot|n't)\s+(?:needed|required|necessary|a must)\b",
    r"\bany\s+{t}\b[^.,;!?]{{0,12}}?\b(?:works|will do|is fine|is ok(?:ay)?)\b",
)


# Words that show a message is about a whole group of conditions. The router sometimes lists
# every group for a message about one ("battery life doesn't matter" must not clear the budget).
_GLOBAL_CLEAR = re.compile(
    r"\b(?:all|every|any)\b[^.,;]{0,30}\b(?:conditions?|filters?|requirements?|restrictions?|limits?|criteria)\b"
    r"|\bstart (?:over|again|fresh)\b|\breset\b",
    re.I,
)
_GROUP_CUES = {
    "budget": r"\$|\b(?:budget|prices?|priced|costs?|spend|money|expensive|cheap\w*|afford\w*)\b",
    "brand": r"\b(?:brands?|makes?|makers?|manufacturers?|labels?)\b",
    "measurements": r"\b(?:limits?|specs?|specifications?|measurements?|sizes?|numbers?)\b",
    "exclusions": r"\b(?:restrictions?|ingredients?|exclusions?|allerg\w+)\b",
    "device": r"\b(?:phones?|devices?|models?|compatib\w+|fits?)\b",
    "features": r"\b(?:features?|extras?)\b",
}


def _group_items(base: ParsedQuery) -> dict[str, list[list[str]]]:
    """For each clearable group, the words that name each condition carried in it."""
    return {
        "budget": [],
        "brand": [[brand] for brand in [*base.brand_include, *base.brand_exclude]],
        "measurements": [_measure_terms(str(c["attribute"])) for c in base.numeric],
        "exclusions": [[term] for term in base.excluded_attributes],
        "device": [[device.split("_")[0]] for device in base.compatible_with],
        "features": [[value] for value in base.features],
    }


def _mentions(query: str, terms: list[str]) -> bool:
    return any(
        re.search(r"\b" + r"[- ]?".join(map(re.escape, re.split(r"[_\s]+", term))) + r"\w*", query, re.I)
        for term in terms if term
    )


def narrow_clears(query: str, base: ParsedQuery | None, clear: list[str] | tuple[str, ...]) -> list[str]:
    """Keep a group clear only when the message is about that whole group.

    The router may list groups the message never touches, and may read "latex is fine" as
    clearing every exclusion. A group stays when the message names it ("any brand", "forget
    the limits") or everything ("drop all the filters"); when it withdraws only some named
    conditions of a group, the merge releases those alone and the group stays untouched.
    """
    text = query or ""
    everything = _GLOBAL_CLEAR.search(text) is not None
    items = _group_items(base) if base is not None else {}
    kept = []
    for group in clear:
        carried = items.get(group, [])
        named = [terms for terms in carried if _mentions(text, terms)]
        about_group = everything or re.search(_GROUP_CUES.get(group, r"(?!)"), text, re.I) is not None
        if not about_group and not named:
            continue  # The message does not touch this group at all.
        if named and len(named) < len(carried) and not about_group:
            continue  # Only some named conditions: released one by one in the merge.
        kept.append(group)
    return kept


def names_carried_condition(query: str, base: ParsedQuery | None) -> bool:
    """Whether the message names a condition this search carries ("I'm ok with alcohol now").

    Such a message may take the condition back, and only the shopper's own words show that:
    a rewrite could restate the condition. Whether it is taken back is the parser's call.
    """
    if base is None:
        return False
    return (
        any(_mentions(query, [term]) for term in base.excluded_attributes)
        or any(_mentions(query, [value]) for value in base.features)
        or any(_mentions(query, _measure_terms(str(c["attribute"]))) for c in base.numeric)
    )


def released(query: str, terms: list[str], rejection: bool = False) -> bool:
    """Whether the shopper withdraws a condition named by any of ``terms``.

    ``rejection`` is true for something the shopper ruled out ("alcohol is fine" takes it back);
    otherwise the condition was a requirement ("battery doesn't matter" drops it).
    """
    text = query or ""
    forms = _ACCEPTS if rejection else _DROPS
    for term in terms:
        words = [re.escape(word) for word in re.split(r"[_\s]+", str(term).strip()) if word]
        if not words:
            continue
        # "noise_cancelling" also reads "noise-cancelling", "noise canceling"; "alcohol-free" is a rejection.
        pattern = r"[- ]?".join(words[:-1] + [words[-1][:6] + r"\w*"]) + r"(?![- ]free)"
        if any(re.search(form.format(t=pattern), text, re.I) for form in forms):
            return True
    return False


_PRICE_PHRASES = (_RANGE, _DASH_RANGE, _MAX, _MIN, _AROUND, _BARE_PRICE)
_NEGATED_PHRASE = re.compile(
    r"\b(?:no|not|without|exclude|excluding|avoid|except|instead of|don'?t want|(?:do|does)(?:n'?t| not) need)\b[^,.;]*", re.I,
)


def _wanted_words(text: str) -> list[str]:
    """What the shopper is looking for, without budgets, rejections or withdrawn conditions."""
    for pattern in _PRICE_PHRASES:
        text = pattern.sub(" ", text)
    text = _NEGATED_PHRASE.sub(" ", text)
    return list(dict.fromkeys(re.sub(r"[,.;]+", " ", text).split()))


def _carried_query(query: str, carried: ParsedQuery | None) -> str:
    """Search text for this turn. Earlier budgets and exclusions live in the structured fields."""
    if carried is None:
        return query
    return " ".join(dict.fromkeys(_wanted_words(query) + _wanted_words(carried.retrieval_query)))


def semantic_query(parsed: ParsedQuery) -> str:
    """Text for embedding and reranking: what is wanted, without budgets or rejections."""
    if parsed.search_text:
        words = parsed.search_text.split()
    else:
        words = _wanted_words(parsed.retrieval_query or parsed.original_query)
    if parsed.sub_category and parsed.sub_category.lower() not in " ".join(words).lower():
        words.insert(0, parsed.sub_category)
    return " ".join(words)


def open_vocabulary(store: Any) -> CatalogVocabulary:
    with closing(store.connect()) as conn:
        return load_vocabulary(conn)
