"""Build inspectable catalog, evaluation, and demo labels from a local Amazon snapshot."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any, Callable

from catalog_config import AMAZON_CATEGORIES, AMAZON_DATA_DIR, STORAGE_DIR
from store.attribute_rules import DESCRIPTION, NUMBER, SEP, TITLE, VOLUME, Rule, bounded, milliliters, rule
from store import attribute_rules


SCHEMA_VERSION = "amazon-labels-v1"
DEFAULT_CASES = Path(__file__).with_name("amazon_label_cases.json")
DEFAULT_OUT = STORAGE_DIR / "amazon" / "labels-v1"
TANK = r"(?:water\s+(?:tank|reservoir))"


def width(match: re.Match[str]) -> str:
    value = match["value"].casefold().replace("-", " ")
    return "extra_wide" if value == "extra wide" else value


COLORS = (
    r"navy blue|rose gold|light blue|dark blue|dark gray|light gray|"
    r"black|white|blue|gray|grey|red|green|pink|purple|silver|gold|brown|orange|yellow|beige"
)
LISTED_COLOR = rule(
    r"\bcolou?r\s*:\s*(?P<value>" + COLORS + r")"
    r"(?!\s*[,/]\s*(?:" + COLORS + r")\b)"
    r"(?=\s*(?:$|[,;|.)\]]|\b(?:size|material|style)\s*:))",
    lambda m: m["value"].casefold().replace("grey", "gray"),
)
ATTRIBUTES: dict[str, dict[str, tuple[Rule, ...]]] = {
    "watch": {
        "gps_mode": (
            rule(r"\b(?:built[- ]in|onboard|integrated|standalone)\s+GPS\b", lambda _: "built_in"),
            rule(r"\b(?:connected GPS|(?:uses?|using)\s+(?:your |the )?(?:smart)?phone['’]s GPS)\b",
                 lambda _: "phone_connected"),
            rule(r"\b(?:no GPS(?: support)?|GPS (?:is )?not supported)\b", lambda _: "not_supported"),
        ),
        "ingress_rating": (rule(r"\b(?P<value>IP[0-6X][0-9X]K?)\b", lambda m: m["value"].upper()),),
        "water_rating_atm": (rule(r"\b" + NUMBER + r"\s*[-–]?\s*ATM\b", bounded(1, 30)),),
        "case_diameter_mm": (
            rule(r"\bcase\s+diameter" + SEP + NUMBER + r"\s*mm\b", bounded(10, 80)),
        ),
        "listed_color": (LISTED_COLOR,),
    },
    "running": {
        "listed_us_size": (
            rule(r"\b(?:US|U\.S\.)\s*(?:men'?s\s*)?(?:size\s*)?" + NUMBER + r"\b(?![\d.])",
                 bounded(3, 20), (TITLE,)),
        ),
        "listed_width": (
            rule(r"\b(?P<value>extra[- ]wide|wide|narrow|standard|medium)\s+width\b", width),
            rule(r"\b(?P<value>extra[- ]wide)\b", width, (TITLE,)),
        ),
        "heel_drop_mm": (
            rule(r"\b(?:midsole|heel[- ]to[- ]toe|heel[- ]toe)\s+drop" + SEP + NUMBER + r"\s*mm\b", bounded(0, 25)),
            rule(r"\b" + NUMBER + r"\s*mm\s+(?:(?:heel[- ]to[- ]toe|midsole)\s+)?drop\b", bounded(0, 25)),
        ),
        "listed_color": (LISTED_COLOR,),
    },
    "coffee": {
        "water_tank_ml": (
            rule(r"\b" + TANK + SEP + r"(?:capacity" + SEP + r")?(?:of\s+)?" + VOLUME, milliliters),
            rule(r"\b" + VOLUME + r"\.?\s+(?:(?:removable|detachable|capacity)\s+){0,2}" + TANK + r"\b", milliliters),
        ),
        "carafe_cups": (
            rule(
                r"\b(?<!\d[-– ])(?P<value>\d{1,2})\s*[-–]?\s*cups?\*?\s+"
                r"(?:(?:programmable|automatic|drip|glass|stainless steel|thermal|switch|compact|double-wall|insulated)\s+){0,4}"
                r"(?:coffee\s*(?:maker|brewer|machine)|coffeemaker|carafe)\b",
                bounded(2, 40),
            ),
        ),
        "pump_pressure_bar": (
            rule(r"\b(?:pump pressure|pressure pump)" + SEP + NUMBER + r"\s*bars?\b", bounded(1, 30)),
            rule(r"\b" + NUMBER + r"\s*[-–]?\s*bars?\s+(?:(?:high[- ]pressure|pressure|Italian|espresso)\s+){0,3}pump\b",
                 bounded(1, 30)),
        ),
        "listed_color": (LISTED_COLOR,),
    },
}


@dataclass
class SourceProduct:
    fact: dict[str, Any]
    text: dict[str, str]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_catalog(source: Path) -> tuple[list[SourceProduct], str]:
    items = []
    seen = set()
    digest = hashlib.sha256()
    for path in sorted(source.glob("*/data/*.json")):
        raw = path.read_bytes()
        digest.update(path.relative_to(source).as_posix().encode() + b"\0" + raw + b"\0")
        product = json.loads(raw)
        meta = product["source"]
        product_id = product["product_id"]
        if product_id in seen or product_id != path.stem or product_id != f"amz_{meta['parent_asin']}":
            raise ValueError(f"Invalid or duplicate product ID: {path}")
        price = Decimal(str(meta["price_usd"]))
        if not price.is_finite() or price <= 0 or not meta["cell"].strip():
            raise ValueError(f"Invalid reference price or category: {product_id}")
        seen.add(product_id)
        items.append(SourceProduct(
            fact={
                "product_id": product_id, "parent_asin": meta["parent_asin"],
                "title": product["title"], "brand": product["brand"],
                "category": AMAZON_CATEGORIES[product["category"]], "category_path": meta["cell"],
                "reference_price_usd": float(price), "provenance": "source_snapshot",
            },
            text={TITLE: product["title"], DESCRIPTION: product["rag_knowledge"]["marketing_description"]},
        ))
    expected = json.loads((source / "sample_report.json").read_text(encoding="utf-8"))["summary"]["products"]
    if not items or len(items) != expected:
        raise ValueError(f"Read {len(items)} products; source report declares {expected}")
    return sorted(items, key=lambda item: item.fact["product_id"]), digest.hexdigest()


def extract_field(item: SourceProduct, rules: tuple[Rule, ...]) -> dict[str, Any]:
    return attribute_rules.extract_field(item.text, rules)


_QA = {
    "gps_mode": ("What GPS mode is mentioned in the listing?", None),
    "ingress_rating": ("What ingress protection rating is listed?", None),
    "water_rating_atm": ("What water-resistance rating is listed in ATM?", " ATM"),
    "case_diameter_mm": ("What is the listed watch case diameter?", " mm"),
    "listed_us_size": ("Which US size is explicitly shown in the listing title?", None),
    "listed_width": ("What shoe width is stated in the listing?", None),
    "heel_drop_mm": ("What is the listed heel-to-toe drop?", " mm"),
    "water_tank_ml": ("What is the stated water tank capacity?", " mL"),
    "carafe_cups": ("How many cups does the listing rate the coffee maker or carafe for?", " cups"),
    "pump_pressure_bar": ("What pump pressure is listed?", " bar"),
    "listed_color": ("Which color is explicitly stated in the listing?", None),
}


def attribute_rows(items: list[SourceProduct], taxonomy: dict[str, str]) -> list[dict[str, Any]]:
    leaf_groups = {taxonomy[group]: group for group in ATTRIBUTES}
    rows = []
    for item in items:
        group = leaf_groups.get(item.fact["category_path"])
        if group is None:
            continue
        rows.append({
            "product_id": item.fact["product_id"], "group": group, "provenance": "rule_extracted_from_listing",
            "attributes": {key: extract_field(item, rules) for key, rules in ATTRIBUTES[group].items()},
        })
    return rows


def catalog_qa(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in attributes:
        for field, label in row["attributes"].items():
            if label["status"] != "observed":
                continue
            question, unit = _QA[field]
            value = label["value"]
            display = f"{value:g}" if isinstance(value, (float, int)) else str(value).replace("_", " ")
            rows.append({
                "product_id": row["product_id"], "attribute": field,
                "question": question, "answer": f"The listing states {display}{unit or ''}.",
                "evidence": label["evidence"], "provenance": "generated_from_listing_attribute",
            })
    return rows


def match_fact(fact: dict[str, Any], criteria: dict[str, Any]) -> bool:
    brand = fact["brand"].casefold()
    price = Decimal(str(fact["reference_price_usd"]))
    return (
        fact["category_path"] in criteria["category_paths"]
        and ("max_usd" not in criteria or price <= Decimal(str(criteria["max_usd"])))
        and ("brands" not in criteria or brand in {b.casefold() for b in criteria["brands"]})
        and brand not in {b.casefold() for b in criteria.get("exclude_brands", [])}
    )


def retrieval_rows(
    items: list[SourceProduct], attributes: list[dict[str, Any]], spec: dict[str, Any],
) -> list[dict[str, Any]]:
    taxonomy = spec["taxonomy"]
    seen_case_ids = set()
    by_product = {row["product_id"]: row["attributes"] for row in attributes}
    rows = []
    for case in spec["retrieval"]:
        case_id = case["id"]
        if case_id in seen_case_ids:
            raise ValueError(f"Duplicate query label: {case_id}")
        seen_case_ids.add(case_id)
        criteria = {k: case[k] for k in ("max_usd", "brands", "exclude_brands", "attribute") if k in case}
        criteria["category_paths"] = [taxonomy[key] for key in case["cells"]]
        hard_paths = {taxonomy[key] for key in case.get("hard_negative_cells", [])}
        known, unknown, hard_negatives = [], [], []
        for item in items:
            fact = item.fact
            pid = fact["product_id"]
            if fact["category_path"] in hard_paths:
                hard_negatives.append(pid)
                continue
            if not match_fact(fact, criteria):
                continue
            target = criteria.get("attribute")
            if not target:
                known.append(pid)
                continue
            observed = by_product.get(pid, {}).get(target["field"], {})
            if observed.get("status") != "observed":
                unknown.append(pid)
                continue
            value = observed["value"]
            if ("equals" not in target or value == target["equals"]) and ("min" not in target or value >= target["min"]):
                known.append(pid)
        if not known and not case.get("out_of_catalog"):
            raise ValueError(f"No verified positive labels for {case_id}; review criteria")
        rows.append({
            "id": case_id, "query": case["query"], "expected_intent": "recommend",
            "criteria": criteria, "out_of_catalog": case.get("out_of_catalog", False),
            "judgment_scope": "partially_observed_attributes" if "attribute" in criteria else "exact_catalog_constraints",
            "relevant_ids": known, "unjudged_ids": unknown, "hard_negative_ids": hard_negatives,
            "provenance": "authored_query_and_catalog_derived_binary_labels",
        })
    return rows


def demo_products(items: list[SourceProduct], running_path: str) -> list[dict[str, Any]]:
    selected = [p.fact for p in items if p.fact["category_path"] == running_path][:24]
    if len(selected) < 24:
        raise ValueError("The demo pack needs at least 24 road-running products")
    rows = []
    for i, fact in enumerate(selected):
        demo_id = "demo_" + fact["product_id"]
        base = 4900 + i * 100
        variants = [
            {"sku_id": f"{demo_id}_{color.lower()}_{size}", "color": color, "size_us": size,
             "price_minor": base + difference, "inventory": inventory}
            for color, size, difference, inventory in (
                ("Black", 8, 0, 5), ("Black", 9, 400, 3), ("Black", 10, 400, 0),
                ("Blue", 9, 900, 2), ("Blue", 11, 1400, 4),
            )
        ]
        rows.append({
            "product_id": demo_id, "source_product_id": fact["product_id"], "currency": "USD",
            "size_order_us": [8, 9, 10, 11], "variants": variants,
            "provenance": "synthetic_transaction_fixture",
        })
    return rows


def interaction_rows(
    qrels: list[dict[str, Any]], fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cases = {row["id"]: row for row in qrels}
    shown = cases["watch-80"]["relevant_ids"][:3]
    first, second, _ = shown
    rows = [
        {"id": "turn-budget", "context": {"search_case_id": "watch-80", "shown_product_ids": shown},
         "turns": [{"query": "At most $50 now", "intent": "refine", "expected_search_case_id": "watch-50"}]},
        {"id": "turn-ordinal", "context": {"shown_product_ids": shown},
         "turns": [{"query": "Compare the second and first", "intent": "compare", "target_product_ids": [second, first]},
                   {"query": "Show details for the second one", "intent": "product_detail", "target_product_ids": [first]}]},
        {"id": "turn-clarify-target", "context": {"shown_product_ids": shown},
         "turns": [{"query": "Can I see the product details?", "intent": "product_detail", "expected_action": "clarify"},
                   {"query": "The second one", "intent": "product_detail", "target_product_ids": [second]}]},
        {"id": "turn-clear-brand", "context": {"search_case_id": "watch-garmin"},
         "turns": [{"query": "Any brand is fine, still no watch bands", "intent": "refine", "expected_search_case_id": "watch-type"}]},
        {"id": "turn-ambiguous", "context": {},
         "turns": [{"query": "I need something", "intent": "clarify", "expected_action": "clarify"},
                   {"query": "Laptop stands at most $50", "intent": "recommend", "expected_search_case_id": "stand-50"}]},
        {"id": "turn-unrelated", "context": {},
         "turns": [{"query": "What's the weather like in San Diego?", "intent": "fallback"}]},
    ]
    for i, fixture in enumerate(fixtures[:4], 1):
        pid = fixture["product_id"]
        variants = {(v["color"], v["size_us"]): v for v in fixture["variants"]}
        black8, black9 = (variants[("Black", n)] for n in (8, 9))
        blue9 = variants[("Blue", 9)]
        rows.extend([
            {"id": f"demo-{i}-clarify", "context": {"shown_product_ids": [pid], "cart": []},
             "turns": [
                 {"query": "Add it to the cart", "intent": "cart", "expected_action": "clarify", "expected_cart_sku_ids": []},
                 {"query": "Black, US 8", "intent": "cart", "expected_action": "add", "expected_cart_sku_ids": [black8["sku_id"]],
                  "expected_unit_price_minor": black8["price_minor"]},
             ]},
            {"id": f"demo-{i}-size", "context": {"cart": [{"sku_id": black8["sku_id"], "quantity": 1}]},
             "turns": [
                 {"query": "Keep it black and go up one size", "intent": "cart", "expected_action": "replace",
                  "expected_cart_sku_ids": [black9["sku_id"]], "expected_unit_price_minor": black9["price_minor"]},
                 {"query": "Go up one more size", "intent": "cart", "expected_action": "reject_out_of_stock",
                  "expected_cart_sku_ids": [black9["sku_id"]]},
             ]},
            {"id": f"demo-{i}-missing-combination", "context": {"cart": [{"sku_id": blue9["sku_id"], "quantity": 1}]},
             "turns": [
                 {"query": "Change it to blue in US 10", "intent": "cart", "expected_action": "reject_missing_variant",
                  "expected_cart_sku_ids": [blue9["sku_id"]]},
             ]},
        ])
    return [{"provenance": "authored_synthetic_test_scenario", **row} for row in rows]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build(source: Path, spec_path: Path, out: Path) -> dict[str, Any]:
    source, spec_path, out = (p.resolve() for p in (source, spec_path, out))
    if out.is_relative_to(source) or out.exists():
        raise FileExistsError(f"Output must be a new directory outside the raw source: {out}")
    items, source_hash = read_catalog(source)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    attributes = attribute_rows(items, spec["taxonomy"])
    qrels = retrieval_rows(items, attributes, spec)
    fixtures = demo_products(items, spec["taxonomy"]["running"])
    scenarios = interaction_rows(qrels, fixtures)
    files = {
        "catalog_facts.jsonl": [item.fact for item in items],
        "catalog_attributes.jsonl": attributes,
        "catalog_qa.jsonl": catalog_qa(attributes),
        "retrieval_labels.jsonl": qrels,
        "interaction_labels.jsonl": scenarios,
        "demo_variants.jsonl": fixtures,
    }
    counts: dict[str, Counter[str]] = {}
    for row in attributes:
        for field, label in row["attributes"].items():
            counts.setdefault(f"{row['group']}.{field}", Counter())[label["status"]] += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".labels-build-", dir=out.parent) as tmp:
        staged = Path(tmp) / "pack"
        staged.mkdir()
        for name, rows in files.items():
            write_jsonl(staged / name, rows)
        manifest = {
            "schema_version": SCHEMA_VERSION, "raw_catalog_sha256": source_hash,
            "case_spec_sha256": file_hash(spec_path),
            "generator_sha256": file_hash(Path(__file__)),
            "evaluation_status": "pilot_not_independently_reviewed",
            "outputs": {name: {"rows": len(rows), "sha256": file_hash(staged / name)} for name, rows in files.items()},
            "attribute_status_counts": {k: dict(v) for k, v in sorted(counts.items())},
            "retrieval_cases_by_scope": dict(Counter(row["judgment_scope"] for row in qrels)),
            "demo_sku_count": sum(len(row["variants"]) for row in fixtures),
            "interaction_turn_count": sum(len(row["turns"]) for row in scenarios),
        }
        (staged / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        staged.rename(out)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=AMAZON_DATA_DIR)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    result = build(args.source, args.cases, args.out_dir)
    print(json.dumps({"output": str(args.out_dir), "counts": {k: v["rows"] for k, v in result["outputs"].items()}}, indent=2))


if __name__ == "__main__":
    main()
