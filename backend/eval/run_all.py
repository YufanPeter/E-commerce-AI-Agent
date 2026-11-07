"""Run all four evaluation suites and print an aggregated summary.

Run from `backend/`:
    python -m eval.run_all
    python -m eval.run_all --skip routing
    python -m eval.run_all --out-dir /tmp/eval_out

Retrieval, routing, and negative-filter evaluations call configured model APIs and require valid
credentials in `.env`. Consistency evaluation is deterministic and does not call an LLM.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from eval._common import hr, pct


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all quantitative evaluations and print a summary")
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        choices=["retrieval", "routing", "consistency", "negative"],
        help="Skip one or more evaluations",
    )
    parser.add_argument("--out-dir", default=None, help="Directory for detailed JSON results")
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    def out_path(name: str) -> str | None:
        if not args.out_dir:
            return None
        os.makedirs(args.out_dir, exist_ok=True)
        return os.path.join(args.out_dir, f"{name}.json")

    results: dict[str, Any] = {}

    if "retrieval" not in args.skip:
        from eval import retrieval_eval
        results["retrieval"] = retrieval_eval.run(top_k=args.top_k, export_json=out_path("retrieval"))

    if "routing" not in args.skip:
        from eval import routing_eval
        results["routing"] = routing_eval.run(export_json=out_path("routing"))

    if "negative" not in args.skip:
        from eval import negative_eval
        results["negative"] = negative_eval.run(top_k=args.top_k, export_json=out_path("negative"))

    if "consistency" not in args.skip:
        from eval import consistency_eval
        results["consistency"] = consistency_eval.run(export_json=out_path("consistency"))

    # ---------------- Aggregate summary ----------------
    print("\n")
    print(hr())
    print("Quantitative evaluation summary")
    print(hr())

    if "retrieval" in results:
        r = results["retrieval"]
        k = r["k"]
        vr, rr = r["vector"]["recall"], r["rerank"]["recall"]
        rel = r["delta"]["recall"]["relative"]
        rel_str = pct(rel) if rel != float("inf") else "—"
        print(f"• RAG Recall@{k}: vector {pct(vr)} -> reranked {pct(rr)} (relative gain +{rel_str})")
        print(f"  MRR：{pct(r['vector']['mrr'])} → {pct(r['rerank']['mrr'])}"
              f"  |  NDCG@{k}: {pct(r['vector']['ndcg'])} -> {pct(r['rerank']['ndcg'])}")

    if "routing" in results:
        r = results["routing"]
        print(f"• Intent-routing accuracy: {pct(r['accuracy'])} ({r['correct']}/{r['total']}, seven tools)")

    if "negative" in results:
        r = results["negative"]
        print(f"• Negative filtering: exclusion success {pct(r['exclude_success_rate'])}, "
              f"violation rate {pct(r['violation_rate'])}")

    if "consistency" in results:
        r = results["consistency"]
        ov = r["overall"]
        print(f"• Factual consistency (price and SKU fields vs SQLite): {pct(ov['rate'])} "
              f"({ov['ok']}/{ov['checks']} checks)")

    print(hr())


if __name__ == "__main__":
    main()
