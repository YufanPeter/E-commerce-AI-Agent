"""Isolate the ranking gain from reranking over vector-distance ordering.

Category-level relevance can saturate retrieval metrics because both paths include many related
items in the top K. This evaluation instead uses semantic queries where only a few products in a
large same-category candidate pool match a target attribute. Distance and reranking operate on the
same retrieved pool, so differences are attributable to reranking.

Run from `backend/`:
    python -m eval.rerank_discrimination_eval
    python -m eval.rerank_discrimination_eval --json out.json
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


def _rank_by_distance(chunks: list[Any]) -> list[str]:
    """Aggregate chunks by product and rank by ascending Chroma distance."""
    best: dict[str, float] = {}
    for c in chunks:
        pid = c.product_id
        if not pid:
            continue
        d = float(c.distance) if c.distance is not None else float("inf")
        if pid not in best or d < best[pid]:
            best[pid] = d
    return [pid for pid, _ in sorted(best.items(), key=lambda kv: kv[1])]


def _rank_by_rerank(reranked: list[Any]) -> list[str]:
    """Aggregate chunks by product and rank by descending reranking score."""
    best: dict[str, float] = {}
    for r in reranked:
        pid = r.product_id
        if not pid:
            continue
        s = float(r.rerank_score)
        if pid not in best or s > best[pid]:
            best[pid] = s
    return [pid for pid, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True)]


def run(top_k_chunks: int = 50, recall_k: int = 3, ndcg_k: int = 5, export_json: str | None = None) -> dict[str, Any]:
    load_env()
    # Route httpx through curl when the local Python TLS stack cannot reach the endpoint.
    from eval._common import install_curl_transport_fallback
    install_curl_transport_fallback()

    from rag.retriever import ChromaRetriever
    from rag.reranker import get_reranker

    dataset = load_dataset("semantic_rerank_labels.json")
    cases = dataset["cases"]

    retriever = ChromaRetriever()
    reranker = get_reranker()

    agg = {
        "vector": {"recall": [], "mrr": [], "ndcg": []},
        "rerank": {"recall": [], "mrr": [], "ndcg": []},
    }
    rows: list[dict[str, Any]] = []

    print(hr())
    print(f"Reranking discrimination  |  semantic queries = {len(cases)}  |  Recall@{recall_k} / MRR / NDCG@{ndcg_k}")
    print("Same-category pools include distractors; compare distance ordering with reranking on gold placement.")
    print(hr())

    for case in cases:
        query = case["query"]
        gold = set(case["gold_ids"])
        category = case["category"]

        # Retrieve by category only, leaving the pool with many same-category distractors.
        chunks = retriever.search(query, top_k=top_k_chunks, where={"category": category})
        vec_rank = _rank_by_distance(chunks)
        reranked = reranker.rerank(query, chunks)
        rer_rank = _rank_by_rerank(reranked)

        rv = recall_at_k(vec_rank, gold, recall_k)
        rr = recall_at_k(rer_rank, gold, recall_k)
        mv = reciprocal_rank(vec_rank, gold)
        mr = reciprocal_rank(rer_rank, gold)
        nv = ndcg_at_k(vec_rank, gold, ndcg_k)
        nr = ndcg_at_k(rer_rank, gold, ndcg_k)

        agg["vector"]["recall"].append(rv); agg["rerank"]["recall"].append(rr)
        agg["vector"]["mrr"].append(mv); agg["rerank"]["mrr"].append(mr)
        agg["vector"]["ndcg"].append(nv); agg["rerank"]["ndcg"].append(nr)

        rows.append({
            "qid": case["qid"], "query": query, "gold": sorted(gold),
            "vector_rank": vec_rank[:ndcg_k], "rerank_rank": rer_rank[:ndcg_k],
            "recall_vector": rv, "recall_rerank": rr,
            "mrr_vector": mv, "mrr_rerank": mr,
        })

        print(f"[{case['qid']}] {query}")
        print(f"    gold({len(gold)}/{case.get('pool_size','?')}): {sorted(gold)}")
        print(f"    Distance top {ndcg_k}: {vec_rank[:ndcg_k]}   Recall@{recall_k}={pct(rv)} MRR={mv:.2f}")
        print(f"    Reranked top {ndcg_k}: {rer_rank[:ndcg_k]}   Recall@{recall_k}={pct(rr)} MRR={mr:.2f}")
        print(hr("-"))

    def avg(path: str, metric: str) -> float:
        return mean(agg[path][metric])

    summary = {
        "recall_k": recall_k, "ndcg_k": ndcg_k, "num_queries": len(cases),
        "vector": {m: avg("vector", m) for m in ("recall", "mrr", "ndcg")},
        "rerank": {m: avg("rerank", m) for m in ("recall", "mrr", "ndcg")},
    }

    print("Summary (annotated-set mean)")
    print(hr("-"))
    print(f"{'Metric':<14}{'Distance':>12}{'Rerank':>12}{'Absolute':>12}{'Relative':>12}")
    for metric, label in (("recall", f"Recall@{recall_k}"), ("mrr", "MRR"), ("ndcg", f"NDCG@{ndcg_k}")):
        b = summary["vector"][metric]; n = summary["rerank"][metric]
        ab = n - b
        rel = (ab / b) if b > 0 else float("inf")
        rel_s = ("+" + pct(rel)) if rel != float("inf") else "—"
        print(f"{label:<14}{pct(b):>12}{pct(n):>12}{('+'+pct(ab)):>12}{rel_s:>12}")
    print(hr())

    summary["delta"] = {
        m: {"absolute": summary["rerank"][m] - summary["vector"][m],
            "relative": ((summary["rerank"][m] - summary["vector"][m]) / summary["vector"][m]) if summary["vector"][m] > 0 else None}
        for m in ("recall", "mrr", "ndcg")
    }

    if export_json:
        with open(export_json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"Detailed results exported to: {export_json}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate reranking discrimination against distance ordering")
    parser.add_argument("--top-k-chunks", type=int, default=50)
    parser.add_argument("--recall-k", type=int, default=3)
    parser.add_argument("--ndcg-k", type=int, default=5)
    parser.add_argument("--json", dest="export_json", default=None)
    args = parser.parse_args()
    run(top_k_chunks=args.top_k_chunks, recall_k=args.recall_k, ndcg_k=args.ndcg_k, export_json=args.export_json)


if __name__ == "__main__":
    main()
