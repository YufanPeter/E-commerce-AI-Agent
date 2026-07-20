"""Evaluate RAG retrieval quality with vector-only and vector-plus-reranking paths.

Recall@K, MRR, and NDCG on a fixed annotated set quantify the gain from reranking.

Run from `backend/`:
    python -m eval.retrieval_eval
    python -m eval.retrieval_eval --top-k 5
    python -m eval.retrieval_eval --json out.json

Both paths share one `ChromaRetriever`; reranking is the only variable so metric differences can
be attributed directly to that stage.
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
    mean,
    ndcg_at_k,
    pct,
    recall_at_k,
    reciprocal_rank,
)


def _predicted_ids(service: Any, query: str, top_k_chunks: int, top_k_products: int) -> list[str]:
    """Run one retrieval and return ranked product IDs."""
    result = service.search(
        query,
        top_k_chunks=top_k_chunks,
        top_k_products=top_k_products,
    )
    return [hit.product_id for hit in result.hits]


def run(top_k: int = 10, top_k_chunks: int = 50, export_json: str | None = None) -> dict[str, Any]:
    load_env()
    # Route httpx through curl when the local Python TLS stack cannot reach the endpoint.
    from eval._common import install_curl_transport_fallback
    install_curl_transport_fallback()

    # Import after loading `.env` so `ChromaRetriever` receives embedding configuration.
    from rag.retriever import ChromaRetriever
    from search.search_service import SearchService

    dataset = load_dataset("retrieval_labels.json")
    cases = dataset["cases"]

    # Both paths share one retriever; `use_rerank` is the only variable.
    shared_retriever = ChromaRetriever()
    svc_vector = SearchService(retriever=shared_retriever, use_rerank=False)
    svc_rerank = SearchService(retriever=shared_retriever, use_rerank=True)

    rows: list[dict[str, Any]] = []
    agg = {
        "vector": {"recall": [], "mrr": [], "ndcg": []},
        "rerank": {"recall": [], "mrr": [], "ndcg": []},
    }

    print(hr())
    print(f"RAG retrieval quality  |  annotated queries = {len(cases)}  |  K = {top_k}  |  chunks = {top_k_chunks}")
    print(hr())
    print(f"{'qid':<26}{'Type':<14}{'Vector R@K':>12}{'+Rerank R@K':>14}")
    print(hr("-"))

    for case in cases:
        query = case["query"]
        relevant = set(case["relevant_ids"])

        pred_v = _predicted_ids(svc_vector, query, top_k_chunks, top_k)
        pred_r = _predicted_ids(svc_rerank, query, top_k_chunks, top_k)

        rv = recall_at_k(pred_v, relevant, top_k)
        rr = recall_at_k(pred_r, relevant, top_k)
        agg["vector"]["recall"].append(rv)
        agg["rerank"]["recall"].append(rr)
        agg["vector"]["mrr"].append(reciprocal_rank(pred_v, relevant))
        agg["rerank"]["mrr"].append(reciprocal_rank(pred_r, relevant))
        agg["vector"]["ndcg"].append(ndcg_at_k(pred_v, relevant, top_k))
        agg["rerank"]["ndcg"].append(ndcg_at_k(pred_r, relevant, top_k))

        rows.append(
            {
                "qid": case["qid"],
                "type": case.get("type", ""),
                "query": query,
                "relevant_ids": sorted(relevant),
                "vector_top": pred_v[:top_k],
                "rerank_top": pred_r[:top_k],
                "recall_vector": rv,
                "recall_rerank": rr,
            }
        )
        print(f"{case['qid']:<26}{case.get('type',''):<14}{pct(rv):>12}{pct(rr):>14}")

    summary = {
        "k": top_k,
        "num_queries": len(cases),
        "vector": {m: mean(agg["vector"][m]) for m in ("recall", "mrr", "ndcg")},
        "rerank": {m: mean(agg["rerank"][m]) for m in ("recall", "mrr", "ndcg")},
    }

    def _delta(metric: str) -> tuple[float, float]:
        base = summary["vector"][metric]
        new = summary["rerank"][metric]
        abs_gain = new - base
        rel_gain = (abs_gain / base) if base > 0 else float("inf")
        return abs_gain, rel_gain

    print(hr())
    print("Summary (annotated-set mean)")
    print(hr("-"))
    print(f"{'Metric':<14}{'Vector only':>14}{'+Rerank':>12}{'Absolute':>12}{'Relative':>12}")
    for metric, label in (("recall", f"Recall@{top_k}"), ("mrr", "MRR"), ("ndcg", f"NDCG@{top_k}")):
        base = summary["vector"][metric]
        new = summary["rerank"][metric]
        abs_gain, rel_gain = _delta(metric)
        rel_str = pct(rel_gain) if rel_gain != float("inf") else "—"
        print(f"{label:<14}{pct(base):>14}{pct(new):>12}{('+'+pct(abs_gain)):>12}{('+'+rel_str):>12}")
    print(hr())

    summary["delta"] = {
        m: {"absolute": _delta(m)[0], "relative": _delta(m)[1]}
        for m in ("recall", "mrr", "ndcg")
    }

    if export_json:
        with open(export_json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"Detailed results exported to: {export_json}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate RAG retrieval quality: reranking vs vector only")
    parser.add_argument("--top-k", type=int, default=10, help="Evaluation K (default: 10)")
    parser.add_argument("--top-k-chunks", type=int, default=50, help="Number of Chroma chunks to retrieve")
    parser.add_argument("--json", dest="export_json", default=None, help="Path for detailed JSON output")
    args = parser.parse_args()
    run(top_k=args.top_k, top_k_chunks=args.top_k_chunks, export_json=args.export_json)


if __name__ == "__main__":
    main()
