"""Evidence-backed attribute extraction from listing text.

A value is recorded only when a rule matches the listing. An absent attribute is unknown:
the listing did not say, which is different from the product lacking it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Any, Callable, Mapping


TITLE = "title"
DESCRIPTION = "rag_knowledge.marketing_description"
NUMBER = r"(?P<value>\d+(?:\.\d+)?)"
SEP = r"\s*[:=–-]?\s*"
VOLUME = NUMBER + r"\s*(?:[-–]\s*)?(?P<unit>fl\.?\s*oz\.?|fluid ounces?|oz\.?|ounces?|mL|millilit(?:er|re)s?|L|lit(?:er|re)s?)\b"
NEGATION = re.compile(r"\b(?:no|not|without|lacks?|not rated(?: for)?)(?:\s+(?:a|an|the|have|with|any|does|do|contain|contains))*\s*$", re.I)
_RANGE_PREFIX = re.compile(r"\b\d+(?:\.\d+)?\s*(?:[-–]|to)\s*$", re.I)

ATTRIBUTE_RULES_VERSION = "attributes-v1"


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    normalize: Callable[[re.Match[str]], str | int | float | None]
    fields: tuple[str, ...]


def rule(
    pattern: str,
    normalize: Callable[[re.Match[str]], str | int | float | None],
    fields: tuple[str, ...] = (TITLE, DESCRIPTION),
) -> Rule:
    return Rule(re.compile(pattern, re.I), normalize, fields)


def bounded(lo: float, hi: float) -> Callable[[re.Match[str]], float | None]:
    def get(match: re.Match[str]) -> float | None:
        value = float(match["value"])
        return value if lo <= value <= hi else None
    return get


def milliliters(match: re.Match[str]) -> float | None:
    unit = match["unit"].casefold().strip(".")
    value = Decimal(match["value"])
    if unit == "l" or unit.startswith(("liter", "litre")):
        value *= 1000
    elif unit != "ml" and not unit.startswith("millilit"):
        value *= Decimal("29.5735295625")
    return float(round(value, 1)) if 5 <= value <= 20000 else None


def constant(value: str) -> Callable[[re.Match[str]], str]:
    return lambda _: value


def _observations(text_by_field: Mapping[str, str], rules: tuple[Rule, ...]) -> dict[str | int | float, dict[str, Any]]:
    found: dict[str | int | float, dict[str, Any]] = {}
    for entry in rules:
        for field in entry.fields:
            text = text_by_field[field]
            for match in entry.pattern.finditer(text):
                context = text[max(0, match.start() - 32):match.start()]
                if NEGATION.search(context):
                    continue
                if match[0][0].isdigit() and _RANGE_PREFIX.search(context):
                    continue
                value = entry.normalize(match)
                if value is None or value in found:
                    continue
                found[value] = {
                    "value": value,
                    "evidence": {"field": field, "start": match.start(), "end": match.end(), "text": match[0]},
                }
    return found


def extract_field(text_by_field: Mapping[str, str], rules: tuple[Rule, ...]) -> dict[str, Any]:
    """Single-valued attribute: conflicting values are ambiguous rather than guessed."""
    observations = _observations(text_by_field, rules)
    if len(observations) == 1:
        return {"status": "observed", **next(iter(observations.values()))}
    if observations:
        return {"status": "ambiguous", "value": None, "observations": list(observations.values())}
    return {"status": "unknown", "value": None}


def extract_multi(text_by_field: Mapping[str, str], rules: tuple[Rule, ...]) -> list[dict[str, Any]]:
    """Multi-valued attribute such as materials or compatible devices."""
    return list(_observations(text_by_field, rules).values())


_FREE_OF: tuple[Rule, ...] = (
    rule(r"\balcohol[- ]free\b", constant("alcohol")),
    rule(r"\b(?:fragrance|scent|perfume)[- ]free\b|\bunscented\b", constant("fragrance")),
    rule(r"\bparaben[- ]free\b", constant("paraben")),
    rule(r"\boil[- ]free\b", constant("oil")),
    rule(r"\bsulfate[- ]free\b|\bsulphate[- ]free\b", constant("sulfate")),
    rule(r"\bsilicone[- ]free\b", constant("silicone")),
    rule(r"\bgluten[- ]free\b", constant("gluten")),
    rule(r"\bbpa[- ]free\b", constant("bpa")),
)
_CONTAINS: tuple[Rule, ...] = (
    rule(r"\b(?:contains?|made with|formulated with)\s+(?:\w+\s+)?alcohol\b|\balcohol denat\b|\bdenatured alcohol\b", constant("alcohol")),
    rule(r"\b(?:contains?|made with|formulated with)\s+(?:\w+\s+)?parabens?\b", constant("paraben")),
    rule(r"\b(?:contains?|made with|formulated with)\s+(?:\w+\s+)?sulfates?\b", constant("sulfate")),
    rule(r"\b(?:contains?|made with|formulated with)\s+(?:\w+\s+)?fragrance\b", constant("fragrance")),
)
_SKIN_TYPE: tuple[Rule, ...] = (
    rule(r"\boily(?:[- ]prone)? skin\b", constant("oily")),
    rule(r"\bdry skin\b", constant("dry")),
    rule(r"\bsensitive skin\b", constant("sensitive")),
    rule(r"\bcombination skin\b", constant("combination")),
    rule(r"\bacne[- ]prone(?: skin)?\b", constant("acne_prone")),
    rule(r"\bmature skin\b", constant("mature")),
)
_CLAIMS: tuple[Rule, ...] = (
    rule(r"\bvegan\b", constant("vegan")),
    rule(r"\bcruelty[- ]free\b", constant("cruelty_free")),
    rule(r"\bhypoallergenic\b", constant("hypoallergenic")),
    rule(r"\bdermatologist[- ]tested\b", constant("dermatologist_tested")),
    rule(r"\bnon[- ]?comedogenic\b", constant("non_comedogenic")),
    rule(r"\breef[- ](?:safe|friendly)\b", constant("reef_safe")),
    rule(r"\bwater[- ]resistant\b", constant("water_resistant")),
    rule(r"\bwaterproof\b", constant("waterproof")),
)
_CONNECTIVITY: tuple[Rule, ...] = (
    rule(r"\bbluetooth\b", constant("bluetooth")),
    rule(r"\bwi-?fi\b", constant("wifi")),
    rule(r"\bwireless\b", constant("wireless")),
    rule(r"\bnfc\b", constant("nfc")),
    rule(r"\busb[- ]?c\b|\btype[- ]c\b", constant("usb_c")),
    rule(r"\bmagsafe\b", constant("magsafe")),
)
_FEATURES: tuple[Rule, ...] = (
    rule(r"\b(?:active )?noise[- ]cancel+(?:ing|ation)\b|\banc\b", constant("noise_cancelling")),
    rule(r"\bfast charg(?:e|ing)\b|\bquick charg(?:e|ing)\b", constant("fast_charging")),
    rule(r"\bbuilt[- ]in (?:mic|microphone)\b|\bwith (?:mic|microphone)\b", constant("microphone")),
    rule(r"\bgps\b", constant("gps")),
    rule(r"\bheart rate\b", constant("heart_rate")),
    rule(r"\bwaterproof\b", constant("waterproof")),
    rule(r"\bwater[- ]resistant\b", constant("water_resistant")),
)
_DEVICES: tuple[Rule, ...] = (
    rule(r"\biphone\s*(?P<value>\d{1,2}(?:\s*(?:pro max|pro|plus|mini))?)\b",
         lambda m: "iphone_" + re.sub(r"\s+", "_", m["value"].casefold())),
    rule(r"\biphone\b", constant("iphone")),
    rule(r"\bipad\b", constant("ipad")),
    rule(r"\bmacbook\b", constant("macbook")),
    rule(r"\bapple watch\b", constant("apple_watch")),
    rule(r"\bairpods\b", constant("airpods")),
    rule(r"\bgalaxy\b|\bsamsung\b", constant("samsung")),
    rule(r"\bandroid\b", constant("android")),
    rule(r"\bfitbit\b", constant("fitbit")),
    rule(r"\bgarmin\b", constant("garmin")),
)
_MATERIAL: tuple[Rule, ...] = (
    rule(r"\b100%\s*cotton\b|\bcotton\b", constant("cotton")),
    rule(r"\bpolyester\b", constant("polyester")),
    rule(r"\bgenuine leather\b|\bleather\b", constant("leather")),
    rule(r"\bstainless steel\b", constant("stainless_steel")),
    rule(r"\baluminum\b|\baluminium\b", constant("aluminum")),
    rule(r"\bceramic\b", constant("ceramic")),
    rule(r"\bsilicone\b", constant("silicone")),
    rule(r"\bnylon\b", constant("nylon")),
    rule(r"\bwool\b", constant("wool")),
    rule(r"\blinen\b", constant("linen")),
    rule(r"\bcast iron\b", constant("cast_iron")),
    rule(r"\bnon[- ]?stick\b", constant("nonstick")),
    rule(r"\bmemory foam\b", constant("memory_foam")),
    rule(r"\bglass\b", constant("glass")),
    rule(r"\bspandex\b|\belastane\b", constant("spandex")),
)
COLOR_WORDS = (
    "black", "white", "navy", "blue", "red", "green", "pink", "purple",
    "gray", "grey", "brown", "beige", "yellow", "orange", "silver", "gold",
)
_COLOR: tuple[Rule, ...] = tuple(
    rule(rf"\b{color}\b", constant("gray" if color == "grey" else color), (TITLE,)) for color in COLOR_WORDS
)

# Attributes that apply to every product in a top-level category.
SINGLE_VALUE: dict[str, dict[str, tuple[Rule, ...]]] = {
    "Beauty & Personal Care": {
        "spf": (rule(r"\bspf\s*-?\s*" + NUMBER + r"\b", bounded(2, 130)),),
        "volume_ml": (rule(r"\b" + VOLUME, milliliters),),
    },
    "Electronics": {
        "battery_hours": (
            rule(r"\b" + NUMBER + r"\s*(?:\+\s*)?(?:hours?|hrs?)\b.{0,25}\bbattery\b", bounded(1, 2000)),
            rule(r"\bbattery\b.{0,25}?\b" + NUMBER + r"\s*(?:\+\s*)?(?:hours?|hrs?)\b", bounded(1, 2000)),
        ),
        "battery_mah": (rule(r"\b" + NUMBER + r"\s*mah\b", bounded(50, 60000)),),
        "wattage": (rule(r"\b" + NUMBER + r"\s*w(?:atts?)?\b", bounded(1, 2000)),),
        "storage_gb": (rule(r"\b" + NUMBER + r"\s*gb\b", bounded(1, 4096)),),
        "cable_length_ft": (rule(r"\b" + NUMBER + r"\s*(?:ft|feet|foot)\b", bounded(0.25, 100)),),
        "ingress_rating": (rule(r"\b(?P<value>IP[0-6X][0-9X]K?)\b", lambda m: m["value"].upper()),),
    },
    "Clothing & Sports": {},
    "Home & Kitchen": {
        "volume_ml": (rule(r"\b" + VOLUME, milliliters),),
        "capacity_cups": (rule(r"\b" + NUMBER + r"\s*[-–]?\s*cups?\b", bounded(1, 60)),),
        "wattage": (rule(r"\b" + NUMBER + r"\s*w(?:atts?)?\b", bounded(1, 5000)),),
    },
}
MULTI_VALUE: dict[str, dict[str, tuple[Rule, ...]]] = {
    "Beauty & Personal Care": {"free_of": _FREE_OF, "contains": _CONTAINS, "skin_type": _SKIN_TYPE, "claim": _CLAIMS},
    "Electronics": {"connectivity": _CONNECTIVITY, "feature": _FEATURES, "compatible_device": _DEVICES, "color": _COLOR},
    "Clothing & Sports": {"material": _MATERIAL, "color": _COLOR, "claim": _CLAIMS},
    "Home & Kitchen": {"material": _MATERIAL, "color": _COLOR, "free_of": _FREE_OF},
}


def extract_attributes(category: str, title: str, description: str) -> list[dict[str, Any]]:
    """Return one row per recorded value, ready for the ``product_attributes`` table."""
    text = {TITLE: title, DESCRIPTION: description}
    rows: list[dict[str, Any]] = []

    def add(attribute: str, status: str, observation: dict[str, Any]) -> None:
        evidence = observation["evidence"]
        rows.append({
            "attribute": attribute, "value": str(observation["value"]), "status": status,
            "evidence_field": evidence["field"], "evidence_start": evidence["start"],
            "evidence_end": evidence["end"], "provenance": "rule",
        })

    for attribute, rules in SINGLE_VALUE.get(category, {}).items():
        result = extract_field(text, rules)
        if result["status"] == "observed":
            add(attribute, "observed", result)
        elif result["status"] == "ambiguous":
            for observation in result["observations"]:
                add(attribute, "ambiguous", observation)
    for attribute, rules in MULTI_VALUE.get(category, {}).items():
        for observation in extract_multi(text, rules):
            add(attribute, "observed", observation)
    return rows
