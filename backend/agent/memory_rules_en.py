"""Plain English statements a shopper makes about themselves, read without a model.

"I have sensitive skin and I never buy Maybelline" holds two facts. Each clause is read on
its own, and what follows "avoid" or "never buy" is checked against the catalog: a brand
it sells is a brand, an ingredient listings state is an ingredient, anything else is left
for the model. Allergies are health facts, so they wait for the shopper to confirm them.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

# Ingredients and properties listings state as contained or free of (product_attributes).
INGREDIENTS = frozenset({
    "alcohol", "almond oil", "aloe vera", "argan oil", "bpa", "ceramides", "charcoal", "cocoa butter", "coconut oil",
    "collagen", "dye", "essential oils", "fragrance", "gluten", "glycerin", "green tea", "honey", "hyaluronic acid",
    "jojoba oil", "keratin", "lanolin", "lead", "niacinamide", "oat", "oil", "oxybenzone", "paraben", "peptides",
    "pfoa", "phthalate", "ptfe", "retinol", "salicylic acid", "shea butter", "silicone", "snail mucin", "sulfate",
    "sulfur", "talc", "tea tree oil", "titanium dioxide", "vitamin a", "vitamin c", "vitamin e", "zinc oxide",
})
_INGREDIENT_ALIASES = {"perfume": "fragrance", "scent": "fragrance", "scents": "fragrance", "fragrances": "fragrance", "dyes": "dye"}

CUES = re.compile(
    r"\b(?:remember|i (?:usually|always|normally|never|don'?t|do not|won'?t|will not|avoid|hate|dislike|prefer|only buy|stick|"
    r"have (?:oily|dry|sensitive|combination|normal) skin|wear)|i'?m allergic|i am allergic|my (?:skin|shoe size|usual budget|budget))\b",
    re.I,
)
_SPLIT = re.compile(r"\s*(?:[,;.!]|\band\b|\bbut\b|\balso\b)\s*", re.I)
_LIKE = re.compile(r"i (?:usually |always |really |mostly )?(?:like|love|prefer|stick (?:with|to)|only buy|always buy) (?P<x>.+)")
_AVOID = re.compile(
    r"i (?:never|don'?t|do not|won'?t|will not) (?:buy|use|wear|want|like)(?: anything)?(?: (?:from|by|with|that has|that have|containing))? (?P<x>.+)"
    r"|i (?:avoid|hate|dislike|stay away from) (?P<y>.+)"
)
_ALLERGY = re.compile(r"(?:i'?m|i am) allergic to (?P<x>.+)|i have an? (?P<y>.+?) allergy")
_SKIN = re.compile(r"(?:i have|i've got) (?P<x>oily|dry|sensitive|combination|normal) skin|my skin is (?:very |quite |really |pretty )?(?P<y>oily|dry|sensitive|combination|normal)")
_SHOE = re.compile(
    r"(?:i (?:usually |always |normally )?wear (?:a )?(?:us |men'?s |women'?s )?size|my shoe size is(?: us)?) ?(?P<x>\d{1,2}(?:\.5)?)(?: (?:in )?(?:shoes?|sneakers?))?"
)
_NOT_NAMES = frozenset({
    "it", "its", "this", "that", "these", "those", "them", "they", "one", "ones", "any", "some", "all", "anything",
    "everything", "something", "nothing", "much", "many", "more", "less", "cheap", "expensive", "new", "old", "big", "small",
})
_FILLER = re.compile(r"^(?:the |any |anything (?:from|by|with) )|(?: (?:products?|brand|stuff|items?|anymore|at all|ever))+$")


@dataclass(frozen=True)
class Statement:
    kind: str  # brand_like, brand_avoid, avoid_term, allergy, skin_type, shoe_size
    value: str


def _clauses(text: str) -> list[str]:
    clean = re.sub(r"^\s*remember(?: that)?\s+", "", text.strip(), flags=re.I)
    clauses = [c.strip().lower() for c in _SPLIT.split(clean) if c and c.strip()]
    # "and I never buy X" splits off "and"; a bare continuation ("never buy X") keeps its subject.
    return [c if c.startswith(("i ", "i'", "my ")) else f"i {c}" if c.startswith(("never ", "avoid ", "prefer ")) else c for c in clauses]


def _thing(raw: str) -> str:
    previous = None
    value = raw.strip(" '\"")
    while value != previous:
        previous, value = value, _FILLER.sub("", value).strip()
    return value


def ingredient(name: str) -> str | None:
    name = _INGREDIENT_ALIASES.get(name, name)
    if name in INGREDIENTS:
        return name
    singular = name[:-1] if name.endswith("s") else name
    return singular if singular in INGREDIENTS else None


def read_statements(text: str, brand_named: Callable[[str], str | None]) -> list[Statement]:
    """Facts about the shopper in ``text``; ``brand_named`` maps words to a catalog brand or None."""
    out: list[Statement] = []
    for clause in _clauses(text):
        if match := _SKIN.fullmatch(clause):
            out.append(Statement("skin_type", match["x"] or match["y"]))
        elif match := _SHOE.fullmatch(clause):
            out.append(Statement("shoe_size", f"US {match['x']}"))
        elif match := _ALLERGY.fullmatch(clause):
            name = _thing(match["x"] or match["y"])
            if found := ingredient(name):
                out.append(Statement("allergy", found))
        elif match := _AVOID.fullmatch(clause):
            name = _thing(match["x"] or match["y"])
            if found := ingredient(name):
                out.append(Statement("avoid_term", found))
            elif brand := brand_named(name):
                out.append(Statement("brand_avoid", brand))
        elif match := _LIKE.fullmatch(clause):
            if brand := brand_named(_thing(match["x"])):
                out.append(Statement("brand_like", brand))
    return list(dict.fromkeys(out))


@lru_cache(maxsize=1)
def _catalog_brands() -> tuple[dict[str, str], tuple[str, ...]]:
    from search.catalog_understanding import brand_key, open_vocabulary
    from store.product_store import ProductStore

    vocabulary = open_vocabulary(ProductStore())
    return dict(vocabulary.brands), tuple(sorted(vocabulary.brands.values(), key=len))


def catalog_brand(name: str) -> str | None:
    """The catalog's spelling of a brand the shopper named, or None when it sells no such brand."""
    from search.catalog_search import same_brand
    from search.catalog_understanding import brand_key

    try:
        by_key, names = _catalog_brands()
    except Exception:  # noqa: BLE001 - without the catalog, leave the sentence to the model
        logger.warning("Loading catalog brands failed", exc_info=True)
        return None
    # "I don't like it" names no brand, even though a brand starts with "It's".
    if not name or len(name) > 40 or len(name) < 2 or name in _NOT_NAMES:
        return None
    return by_key.get(brand_key(name)) or next((brand for brand in names if same_brand(brand, name)), None)
