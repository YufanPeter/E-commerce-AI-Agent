"""Deterministic simulated SKUs, prices, and stock for the Amazon snapshot.

The dataset has no purchasable variants or inventory, so this layer invents them for the
demo transaction flow. Every SKU ID starts with ``sim_`` and the build records
``commerce_source``. The cheapest SKU always equals the source price and is always in
stock, so budget filters agree with the catalog and every product can be bought.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from itertools import product as cartesian
import json
from pathlib import Path
import random
from typing import Any


RULES_PATH = Path(__file__).resolve().parents[1] / "db" / "amazon_variant_rules.json"
OUT_OF_STOCK_RATE = 0.09
_CENT = Decimal("0.01")
_STANDARD = {"name": "Option", "values": ["Standard"], "mode": "add", "steps": [0]}


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _dimension(key: str, rules: dict[str, Any], rng: random.Random, observed_colors: list[str]) -> dict[str, Any]:
    """Resolve one dimension, preferring values the listing actually states."""
    spec = rules["dimensions"][key]
    if spec.get("palette"):
        count = spec.get("pick", 3)
        values = list(dict.fromkeys(observed_colors))[:count]
        remaining = [color for color in rules["palette"] if color not in values]
        rng.shuffle(remaining)
        values += remaining[:max(0, count - len(values))]
        return {"name": spec["name"], "values": values, "mode": "add", "steps": [0] * len(values)}
    indexed = list(range(len(spec["values"])))
    if "pick" in spec:
        indexed = sorted(rng.sample(indexed, spec["pick"]))
        if 0 not in indexed:  # The first value carries the zero step that anchors the base price.
            indexed = [0, *indexed[:-1]]
    return {
        "name": spec["name"],
        "values": [spec["values"][i] for i in indexed],
        "mode": spec["mode"],
        "steps": [spec["steps"][i] for i in indexed],
    }


def _price(base: Decimal, dimensions: list[dict[str, Any]], choice: tuple[int, ...]) -> Decimal:
    price = base
    for dimension, index in zip(dimensions, choice):
        step = Decimal(str(dimension["steps"][index]))
        price = price * step if dimension["mode"] == "multiply" else price + step
    return price.quantize(_CENT, rounding=ROUND_HALF_UP)


def _stock(rng: random.Random, is_base: bool) -> int:
    if is_base:
        return rng.randint(6, 200)
    roll = rng.random()
    if roll < OUT_OF_STOCK_RATE:
        return 0
    if roll < OUT_OF_STOCK_RATE + 0.2:
        return rng.randint(1, 5)
    return rng.randint(6, 200)


def synthesize_skus(
    asin: str,
    label: str,
    price_usd: float,
    observed_colors: list[str],
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the SKU rows for one product; output depends only on the arguments."""
    rng = random.Random(f"{rules['version']}:{asin}")
    keys = rules["labels"].get(label, [])
    dimensions = [_dimension(key, rules, rng, observed_colors) for key in keys] or [_STANDARD]
    base = Decimal(str(price_usd))
    rows = []
    for number, choice in enumerate(cartesian(*(range(len(d["values"])) for d in dimensions))):
        is_base = not any(choice)
        rows.append({
            "sku_id": f"sim_{asin}_{number}",
            "properties": {d["name"]: d["values"][i] for d, i in zip(dimensions, choice)},
            "price": float(_price(base, dimensions, choice)),
            "stock_qty": _stock(rng, is_base),
        })
    return rows
