"""Measured attributes shoppers can limit, read from one schema file.

The schema decides which attribute a unit belongs to, so parsing, checking model output
and filtering products share one definition instead of per-phrase rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "amazon_numeric_attributes.json"
OPS = (">=", "<=")
NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?|\.\d+"
_TOLERANCE = 0.02


@dataclass(frozen=True)
class NumericAttribute:
    name: str
    label: str
    unit: str
    units: dict[str, float]
    prefix_units: tuple[str, ...]


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def numeric_attributes() -> dict[str, NumericAttribute]:
    return {
        name: NumericAttribute(name, spec["label"], spec["unit"], spec["units"], tuple(spec.get("prefix_units", ())))
        for name, spec in _schema()["attributes"].items()
    }


def _alternation(units: Iterable[str]) -> str:
    return "|".join(re.escape(unit).replace(r"\ ", r"\s*") for unit in sorted(set(units), key=len, reverse=True))


@lru_cache(maxsize=1)
def _unit_owner() -> dict[str, str]:
    owner: dict[str, str] = {}
    for attribute in numeric_attributes().values():
        for unit in attribute.units:
            if unit in owner:
                raise ValueError(f"Unit {unit!r} belongs to both {owner[unit]} and {attribute.name}")
            owner[unit] = attribute.name
    return owner


@lru_cache(maxsize=1)
def unit_pattern() -> str:
    """Every unit a number can carry, for telling measurements from prices."""
    symbols = [u for u in _schema()["other_units"] if not u[0].isalnum()]
    words = [*_unit_owner(), *(u for u in _schema()["other_units"] if u[0].isalnum())]
    return rf"(?:{_alternation(symbols)}|(?:{_alternation(words)})\b)" if symbols else rf"(?:{_alternation(words)})\b"


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _key(unit: str) -> str:
    return re.sub(r"\s+", " ", unit.casefold())


@lru_cache(maxsize=1)
def _mentions() -> re.Pattern[str]:
    owner = _unit_owner()
    prefixes = [u for a in numeric_attributes().values() for u in a.prefix_units]
    suffixes = [u for u in owner if u not in prefixes]
    return re.compile(
        rf"(?<![\w.])(?P<num>{NUMBER})\s*-?\s*(?P<unit>{_alternation(suffixes)})\b"
        rf"|\b(?P<pre>{_alternation(prefixes)})\s*-?\s*(?P<pnum>{NUMBER})(?![\w.])",
        re.I,
    )


_AT_LEAST_BEFORE = re.compile(r"(?:at least|minimum(?: of)?|min\.?|over|more than|above|greater than|no less than|longer than|bigger than)\s*$", re.I)
_AT_MOST_BEFORE = re.compile(r"(?:up to|at most|under|below|less than|no more than|maximum(?: of)?|max\.?|within|shorter than|smaller than)\s*$", re.I)
_AT_LEAST_AFTER = re.compile(r"^\s*(?:\+|or (?:more|higher|above|longer|bigger|larger|greater)\b|plus\b|minimum\b)", re.I)
_AT_MOST_AFTER = re.compile(r"^\s*(?:or (?:less|lower|under|shorter|smaller|fewer)\b|max(?:imum)?\b)", re.I)


def rule_constraints(text: str) -> list[dict[str, Any]]:
    """Read limits such as 'at least 20 hours' or 'SPF 50 or higher'; a bare amount is no limit."""
    out: list[dict[str, Any]] = []
    owner = _unit_owner()
    for match in _mentions().finditer(text):
        unit = _key(match["unit"] or match["pre"])
        attribute = numeric_attributes()[owner[unit]]
        value = _number(match["num"] or match["pnum"]) * attribute.units[unit]
        before, after = text[:match.start()], text[match.end():]
        if _AT_LEAST_BEFORE.search(before) or _AT_LEAST_AFTER.search(after):
            op = ">="
        elif _AT_MOST_BEFORE.search(before) or _AT_MOST_AFTER.search(after):
            op = "<="
        else:
            continue
        out.append({"attribute": attribute.name, "op": op, "value": round(value, 2)})
    return out


def stated_constraint(raw: Any, text: str) -> dict[str, Any] | None:
    """Accept a model constraint only for a known attribute and an amount the shopper wrote."""
    if not isinstance(raw, dict):
        return None
    attribute = numeric_attributes().get(str(raw.get("attribute")))
    op = raw.get("op")
    try:
        value = float(raw.get("value"))
    except (TypeError, ValueError):
        return None
    if attribute is None or op not in OPS or value <= 0:
        return None
    written = [_number(n) for n in re.findall(NUMBER, text)]
    factors = set(attribute.units.values())
    if not any(abs(n * f - value) <= _TOLERANCE * value for n in written for f in factors):
        return None
    return {"attribute": attribute.name, "op": op, "value": round(value, 2)}


def satisfies(values: Iterable[str], op: str, limit: float) -> bool | None:
    """True if any stated value meets the limit, False if all miss it, None if nothing is stated."""
    numbers = []
    for value in values:
        try:
            numbers.append(float(value))
        except ValueError:
            continue
    if not numbers:
        return None
    return any(n >= limit if op == ">=" else n <= limit for n in numbers)


def describe(constraint: dict[str, Any]) -> str:
    """Stable label shown to the copywriter, such as 'battery_hours >= 20'."""
    return f"{constraint['attribute']} {constraint['op']} {constraint['value']:g}"
