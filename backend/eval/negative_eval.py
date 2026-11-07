"""Evaluate exclusion filtering for requests that prohibit an ingredient or product attribute.

For annotated queries with negative constraints, the evaluation checks that prohibited products
are removed while explicitly allowed products remain available. The resulting violation and
retention rates quantify the exclusion-filtering behavior.

Run from `backend/`:
    python -m eval.negative_eval
    python -m eval.negative_eval --json out.json
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from eval._common import (
    BACKEND_DIR,  # noqa: F401  Trigger sys.path setup.
    hr,
    load_dataset,
    load_env,
    pct,
)


def run(top_k: int = 10, export_json: str | None = None) -> dict[str, Any]:
    load_env()
    # Route httpx through curl when the local Python TLS stack cannot reach the endpoint.
    from eval._common import install_curl_transport_fallback
    install_curl_transport_fallback()

    from search.search_service import SearchService

    dataset = load_dataset("negative_labels.json")
    cases = dataset["cases"]
    service = SearchService(use_rerank=True)

    rows: list[dict[str, Any]] = []
    total_excluded_checks = 0
    excluded_success = 0          # Expected exclusions that did not appear.
    total_keep_checks = 0
    keep_success = 0              # Expected retained products found within the top K.
    queries_clean = 0             # Queries with no exclusion violations.

    print(hr())
    print(f"Negative-constraint filtering  |  queries = {len(cases)}  |  K = {top_k}")
    print(hr())

    for case in cases:
        query = case["query"]
        must_exclude = set(case.get("must_exclude", []))
        should_include = set(case.get("should_include_if_present", []))

        # Inject annotated negative ingredients through `base` to trigger filtering deterministically.
        # This evaluation measures filtering rather than variance in one LLM extraction call.
        base = None
        force_negatives = case.get("force_negatives")
        if force_negatives:
            from search.query_understanding import ParsedQuery

            base = ParsedQuery(original_query=query, negative_ingredients=list(force_negatives))

        result = service.search(query, top_k_products=top_k, base=base)
        returned_ids = [h.product_id for h in result.hits]
        returned_set = set(returned_ids)
        parsed = result.parsed
        negatives = list(getattr(parsed, "negative_ingredients", []) or [])

        # Validate required exclusions.
        violated = sorted(must_exclude & returned_set)
        for _ in must_exclude:
            total_excluded_checks += 1
        excluded_success += len(must_exclude) - len(violated)

        # Validate allowed products that explicitly do not contain the prohibited ingredient.
        kept = sorted(should_include & returned_set)
        for _ in should_include:
            total_keep_checks += 1
        keep_success += len(kept)

        clean = len(violated) == 0
        queries_clean += int(clean)

        rows.append({
            "qid": case["qid"],
            "query": query,
            "parsed_negatives": negatives,
            "returned_ids": returned_ids,
            "must_exclude": sorted(must_exclude),
            "violations": violated,
            "kept_allowed": kept,
            "clean": clean,
        })

        print(f"[{case['qid']}] {query}")
        print(f"    Parsed negative ingredients: {negatives or '(none)'}")
        print(f"    Returned top {top_k}: {returned_ids}")
        print(f"    Must exclude: {sorted(must_exclude)}  ->  violations: {violated or 'none'}")
        if should_include:
            print(f"    Should remain eligible: {sorted(should_include)}  ->  retained: {kept}")
        print(hr("-"))

    exclude_rate = excluded_success / total_excluded_checks if total_excluded_checks else 0.0
    keep_rate = keep_success / total_keep_checks if total_keep_checks else 0.0
    violation_rate = 1.0 - exclude_rate
    clean_rate = queries_clean / len(cases) if cases else 0.0

    print("Summary")
    print(hr("-"))
    print(f"Successful exclusion rate : {pct(exclude_rate)}  ({excluded_success}/{total_excluded_checks})")
    print(f"Violation rate            : {pct(violation_rate)}")
    print(f"Allowed-product retention : {pct(keep_rate)}  ({keep_success}/{total_keep_checks})")
    print(f"Violation-free queries     : {pct(clean_rate)}  ({queries_clean}/{len(cases)})")
    print(hr())

    summary = {
        "k": top_k,
        "num_queries": len(cases),
        "exclude_success_rate": exclude_rate,
        "violation_rate": violation_rate,
        "keep_rate": keep_rate,
        "clean_query_rate": clean_rate,
    }

    if export_json:
        with open(export_json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"Detailed results exported to: {export_json}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate negative-constraint filtering")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", dest="export_json", default=None, help="Path for detailed JSON output")
    args = parser.parse_args()
    run(top_k=args.top_k, export_json=args.export_json)


if __name__ == "__main__":
    main()
