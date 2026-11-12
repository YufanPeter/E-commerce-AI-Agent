"""Score English query understanding on the Amazon catalog.

Runs each case's turns through the same parse and merge steps that search uses, then
checks the listed fields of the final intent. No retrieval runs, so a full pass costs
one fast-model call per turn (cached within the process).

    CARTPILOT_CATALOG=amazon python -m eval.query_understanding_eval [--rules] [--out report.json]
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from eval._common import load_dataset, load_env

FIELDS = (
    "type", "min", "max", "brand_in", "brand_out", "excl_attr", "excl_type", "numeric", "features", "devices",
    "unavailable", "text_has", "text_lacks",
)


def _observed(parsed: Any, text: str) -> dict[str, Any]:
    return {
        "type": parsed.sub_category,
        "min": parsed.min_price,
        "max": parsed.max_price,
        "brand_in": sorted(b.casefold() for b in parsed.brand_include),
        "brand_out": sorted(b.casefold() for b in parsed.brand_exclude),
        "excl_attr": sorted(parsed.excluded_attributes),
        "excl_type": sorted(parsed.sub_category_exclude),
        "numeric": sorted([c["attribute"], c["op"], float(c["value"])] for c in getattr(parsed, "numeric", [])),
        "features": sorted(getattr(parsed, "features", [])),
        "devices": sorted(parsed.compatible_with),
        "unavailable": bool(parsed.unavailable_terms),
        "text": text,
    }


def _passes(field: str, expected: Any, seen: dict[str, Any]) -> bool:
    if field == "type":
        return seen["type"] in expected
    if field in ("min", "max"):
        return seen[field] == (None if expected is None else float(expected))
    if field in ("brand_in", "brand_out"):
        return seen[field] == sorted(b.casefold() for b in expected)
    if field in ("excl_attr", "excl_type", "features", "devices"):
        return seen[field] == sorted(expected)
    if field == "numeric":
        return seen["numeric"] == sorted([a, op, float(v)] for a, op, v in expected)
    if field == "unavailable":
        return seen["unavailable"] == expected
    text = seen["text"].casefold()
    if field == "text_has":
        return all(word.casefold() in text for word in expected)
    return not any(word.casefold() in text for word in expected)  # text_lacks


def run(rules_only: bool = False) -> dict[str, Any]:
    from catalog_config import PROFILE
    from search import catalog_understanding as cu
    from store.product_store import ProductStore

    vocabulary = cu.open_vocabulary(ProductStore(PROFILE.db_path))
    cases = load_dataset("amazon_query_cases.json")["cases"]
    totals, failures = Counter(), []
    with patch.object(cu, "_call_model", side_effect=TimeoutError("rules only")) if rules_only else _noop():
        for case in cases:
            base = None
            for turn in case["turns"]:
                current = cu.understand(turn, vocabulary, base=base) if _takes_base(cu.understand) else cu.understand(turn, vocabulary)
                base = cu.merge_turn(current, base, {})
            seen = _observed(base, cu.semantic_query(base))
            wrong = [f for f in FIELDS if f in case["expect"] and not _passes(f, case["expect"][f], seen)]
            for field in case["expect"]:
                totals[(field, field not in wrong)] += 1
            totals[("case", not wrong)] += 1
            if wrong:
                failures.append({"id": case["id"], "turns": case["turns"], "wrong": wrong,
                                 "expected": {f: case["expect"][f] for f in wrong}, "seen": seen})
    by_field = {
        field: f"{totals[(field, True)]}/{totals[(field, True)] + totals[(field, False)]}"
        for field in ("case", *FIELDS) if totals[(field, True)] + totals[(field, False)]
    }
    return {"mode": "rules" if rules_only else "model", "by_field": by_field, "failures": failures}


def _takes_base(function: Any) -> bool:
    import inspect
    return "base" in inspect.signature(function).parameters


class _noop:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", action="store_true", help="Disable the model to score the fallback path.")
    parser.add_argument("--out", type=Path, help="Write the full report as JSON.")
    args = parser.parse_args()
    load_env()
    report = run(args.rules)
    print(json.dumps(report["by_field"], indent=1))
    for failure in report["failures"]:
        print(f"- {failure['id']}: {failure['wrong']} expected={failure['expected']} "
              f"seen={ {k: failure['seen'][k] for k in failure['wrong'] if k in failure['seen']} } text={failure['seen']['text']!r}")
    if args.out:
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
