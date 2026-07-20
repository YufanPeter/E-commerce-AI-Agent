"""Compare keyword, semantic, hybrid, and full-RAG retrieval quality.

Colloquial user intent may not share literal terms with professional catalog copy. This evaluation
measures whether embeddings recover that semantic relationship and whether query understanding,
hard filters, vector retrieval, and reranking improve it further.

Retrieval paths over the complete catalog:
    1. keyword: dependency-free BM25 with character-bigram tokenization
    2. semantic: unrestricted Chroma vector retrieval
    3. hybrid: reciprocal-rank fusion of BM25 and vector rankings
    4. full_rag: the complete `SearchService` pipeline

Run from `backend/`:
    python -m eval.semantic_value_eval
    python -m eval.semantic_value_eval --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
from collections import Counter
from typing import Any, Callable

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


# ---------------------------------------------------------------------------
# Dependency-free CJK tokenization and BM25 using character bigrams.
# ---------------------------------------------------------------------------

_CJK = r"\u4e00-\u9fff"


def tokenize(text: str) -> list[str]:
    """Tokenize Latin text and numbers by word, and CJK text with adjacent character bigrams."""
    text = (text or "").lower()
    tokens: list[str] = []
    for m in re.finditer(r"[a-z0-9]+", text):
        tokens.append(m.group())
    for seg in re.findall(rf"[{_CJK}]+", text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i : i + 2])
    return tokens


class BM25:
    """Dependency-free implementation of standard BM25 Okapi."""

    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs = corpus_tokens
        self.N = len(corpus_tokens)
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        self.doc_freqs = [Counter(d) for d in corpus_tokens]
        df: Counter = Counter()
        for d in corpus_tokens:
            for t in set(d):
                df[t] += 1
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()
        }

    def score(self, query_tokens: list[str], idx: int) -> float:
        freqs = self.doc_freqs[idx]
        dl = self.doc_len[idx] or 1
        s = 0.0
        for t in query_tokens:
            if t not in freqs:
                continue
            f = freqs[t]
            idf = self.idf.get(t, 0.0)
            s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    def rank(self, query: str) -> list[int]:
        q = tokenize(query)
        scored = [(i, self.score(q, i)) for i in range(self.N)]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [i for i, sc in scored if sc > 0]


def _load_corpus(db_path: str) -> tuple[list[str], list[str]]:
    """Load catalog text from titles, marketing descriptions, and official FAQs."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    pids: list[str] = []
    docs: list[str] = []
    rows = conn.execute(
        "SELECT product_id, title FROM products WHERE status='active' ORDER BY product_id"
    ).fetchall()
    for r in rows:
        pid = r["product_id"]
        d = conn.execute(
            "SELECT marketing_description FROM product_descriptions WHERE product_id=?", (pid,)
        ).fetchone()
        faqs = conn.execute(
            "SELECT group_concat(question || ' ' || answer, ' ') t FROM product_faqs WHERE product_id=?",
            (pid,),
        ).fetchone()
        text = " ".join(
            [
                r["title"] or "",
                (d["marketing_description"] if d else "") or "",
                (faqs["t"] if faqs and faqs["t"] else ""),
            ]
        )
        pids.append(pid)
        docs.append(text)
    conn.close()
    return pids, docs


# ---------------------------------------------------------------------------
# Product-level ranking for vector retrieval and full RAG.
# ---------------------------------------------------------------------------


def _retry(fn: Callable[[], Any], tries: int = 5, base_delay: float = 3.0) -> Any:
    """Retry network calls with incremental backoff for transient embedding or LLM failures."""
    last: Exception | None = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - Evaluation scripts retry transient network errors.
            last = exc
            if i < tries - 1:
                time.sleep(base_delay * (i + 1))
    raise last  # type: ignore[misc]


def _vector_rank(retriever: Any, query: str, top_k_chunks: int) -> list[str]:
    """Retrieve unrestricted semantic chunks and aggregate products by minimum distance."""
    chunks = _retry(lambda: retriever.search(query, top_k=top_k_chunks, where=None))
    best: dict[str, float] = {}
    for c in chunks:
        pid = c.product_id
        if not pid:
            continue
        d = float(c.distance) if c.distance is not None else float("inf")
        if pid not in best or d < best[pid]:
            best[pid] = d
    return [pid for pid, _ in sorted(best.items(), key=lambda kv: kv[1])]


def _rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Combine multiple rankings with reciprocal rank fusion.

    Each ranking contributes `1 / (k + rank)` per product; contributions are summed and sorted.
    The conventional `k=60` avoids normalizing incomparable BM25 and vector-distance scores.
    """
    score: dict[str, float] = {}
    for ranking in rankings:
        for rank, pid in enumerate(ranking, start=1):
            if not pid:
                continue
            score[pid] = score.get(pid, 0.0) + 1.0 / (k + rank)
    return [pid for pid, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)]


def run(recall_k: int = 5, ndcg_k: int = 5, top_k_chunks: int = 400, export_json: str | None = None) -> dict[str, Any]:
    load_env()
    # Install the transport fallback for transient TLS failures in embedding requests.
    from eval._common import install_requests_embedding_fallback
    install_requests_embedding_fallback()

    from rag.retriever import ChromaRetriever
    from search.search_service import SearchService
    from store.product_store import ProductStore

    dataset = load_dataset("semantic_intent_labels.json")
    cases = dataset["cases"]

    db_path = str(ProductStore().db_path)
    pids, docs = _load_corpus(db_path)
    bm25 = BM25([tokenize(t) for t in docs])
    id_by_idx = pids

    retriever = ChromaRetriever()
    service = SearchService(use_rerank=True)

    agg = {m: {"recall": [], "mrr": [], "ndcg": []} for m in ("keyword", "semantic", "hybrid", "full_rag")}
    rows: list[dict[str, Any]] = []

    print(hr())
    print(f"RAG retrieval value  |  queries = {len(cases)}  |  unrestricted full-catalog search")
    print(f"Compare: 1. BM25  2. semantic vectors  3. hybrid RRF  4. full RAG   Metrics: Recall@{recall_k}/MRR/NDCG@{ndcg_k}")
    print(hr())

    for case in cases:
        query = case["query"]
        gold = set(case["gold_ids"])

        # 1. Keyword BM25
        kw_rank = [id_by_idx[i] for i in bm25.rank(query)]
        # 2. Semantic vectors
        vec_rank = _vector_rank(retriever, query, top_k_chunks)
        # 3. Hybrid reciprocal-rank fusion of BM25 and vector results
        hybrid_rank = _rrf_fuse([kw_rank, vec_rank])
        # 4. Full RAG pipeline
        rag_rank = [h.product_id for h in _retry(lambda: service.search(query, top_k_products=10)).hits]

        for name, ranked in (("keyword", kw_rank), ("semantic", vec_rank), ("hybrid", hybrid_rank), ("full_rag", rag_rank)):
            agg[name]["recall"].append(recall_at_k(ranked, gold, recall_k))
            agg[name]["mrr"].append(reciprocal_rank(ranked, gold))
            agg[name]["ndcg"].append(ndcg_at_k(ranked, gold, ndcg_k))

        rows.append({
            "qid": case["qid"], "query": query, "gold": sorted(gold),
            "keyword_top5": kw_rank[:5], "semantic_top5": vec_rank[:5],
            "hybrid_top5": hybrid_rank[:5], "full_rag_top5": rag_rank[:5],
        })

        print(f"[{case['qid']}] {query}")
        print(f"    Catalog terminology: {case.get('term_in_doc','')}  gold={sorted(gold)}")
        print(f"    1. BM25       top5: {kw_rank[:5]}  R@{recall_k}={pct(recall_at_k(kw_rank, gold, recall_k))}")
        print(f"    2. Semantic   top5: {vec_rank[:5]}  R@{recall_k}={pct(recall_at_k(vec_rank, gold, recall_k))}")
        print(f"    3. Hybrid     top5: {hybrid_rank[:5]}  R@{recall_k}={pct(recall_at_k(hybrid_rank, gold, recall_k))}")
        print(f"    4. Full RAG   top5: {rag_rank[:5]}  R@{recall_k}={pct(recall_at_k(rag_rank, gold, recall_k))}")
        print(hr("-"))

    summary = {
        "recall_k": recall_k, "ndcg_k": ndcg_k, "num_queries": len(cases),
        **{name: {m: mean(agg[name][m]) for m in ("recall", "mrr", "ndcg")} for name in agg},
    }

    print("Summary (annotated-set mean)")
    print(hr("-"))
    print(f"{'Retrieval method':<22}{f'Recall@{recall_k}':>12}{'MRR':>10}{f'NDCG@{ndcg_k}':>12}")
    for name, label in (("keyword", "1. Keyword BM25"), ("semantic", "2. Semantic vector"), ("hybrid", "3. Hybrid (RRF)"), ("full_rag", "4. Full RAG")):
        s = summary[name]
        print(f"{label:<22}{pct(s['recall']):>12}{pct(s['mrr']):>10}{pct(s['ndcg']):>12}")
    print(hr("-"))

    # Relative improvements between retrieval paths.
    def rel(metric: str, a: str, b: str) -> str:
        base = summary[b][metric]
        new = summary[a][metric]
        if base <= 0:
            return "—" if new <= 0 else "∞"
        return "+" + pct((new - base) / base)

    print("Semantic vector improvement over keyword BM25:")
    print(f"    Recall@{recall_k}: {rel('recall','semantic','keyword')}   "
          f"MRR: {rel('mrr','semantic','keyword')}   NDCG@{ndcg_k}: {rel('ndcg','semantic','keyword')}")
    print("Hybrid RRF improvement over keyword BM25:")
    print(f"    Recall@{recall_k}: {rel('recall','hybrid','keyword')}   "
          f"MRR: {rel('mrr','hybrid','keyword')}   NDCG@{ndcg_k}: {rel('ndcg','hybrid','keyword')}")
    print("Hybrid RRF improvement over semantic vectors:")
    print(f"    Recall@{recall_k}: {rel('recall','hybrid','semantic')}   "
          f"MRR: {rel('mrr','hybrid','semantic')}   NDCG@{ndcg_k}: {rel('ndcg','hybrid','semantic')}")
    print("Full RAG improvement over keyword BM25:")
    print(f"    Recall@{recall_k}: {rel('recall','full_rag','keyword')}   "
          f"MRR: {rel('mrr','full_rag','keyword')}   NDCG@{ndcg_k}: {rel('ndcg','full_rag','keyword')}")
    print(hr())

    summary["relative"] = {
        "semantic_vs_keyword": {m: rel(m, "semantic", "keyword") for m in ("recall", "mrr", "ndcg")},
        "hybrid_vs_keyword": {m: rel(m, "hybrid", "keyword") for m in ("recall", "mrr", "ndcg")},
        "hybrid_vs_semantic": {m: rel(m, "hybrid", "semantic") for m in ("recall", "mrr", "ndcg")},
        "full_rag_vs_keyword": {m: rel(m, "full_rag", "keyword") for m in ("recall", "mrr", "ndcg")},
    }

    if export_json:
        with open(export_json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"Detailed results exported to: {export_json}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate keyword, semantic, hybrid, and full-RAG retrieval")
    parser.add_argument("--recall-k", type=int, default=5)
    parser.add_argument("--ndcg-k", type=int, default=5)
    parser.add_argument("--top-k-chunks", type=int, default=400)
    parser.add_argument("--json", dest="export_json", default=None)
    args = parser.parse_args()
    run(recall_k=args.recall_k, ndcg_k=args.ndcg_k, top_k_chunks=args.top_k_chunks, export_json=args.export_json)


if __name__ == "__main__":
    main()
