from __future__ import annotations

"""Dependency-free BM25 retrieval and RRF fusion for hybrid search.

Chinese text uses character bigrams, which is sufficient for this catalog and keeps
offline evaluation consistent with production. BM25 is calculated over the filtered
candidate subset, avoiding a separate filter-aware inverted-index service.
"""

import math
import re
from collections import Counter
from typing import Iterable

_CJK = r"\u4e00-\u9fff"


def tokenize(text: str) -> list[str]:
    """Tokenize Latin text by word and Chinese text by adjacent-character bigrams."""
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
    """Standard BM25 Okapi with corpus order aligned to external IDs."""

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
        """Return positive-scoring document indices in descending order."""
        q = tokenize(query)
        scored = [(i, self.score(q, i)) for i in range(self.N)]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [i for i, sc in scored if sc > 0]


def rank_ids_bm25(query: str, ids: list[str], docs: list[str]) -> list[str]:
    """Calculate BM25 over aligned IDs and documents and return ranked IDs.

    This is used for sparse retrieval over a filtered candidate subset.
    """
    if not ids:
        return []
    bm25 = BM25([tokenize(d) for d in docs])
    return [ids[i] for i in bm25.rank(query)]


def rrf_fuse(rankings: Iterable[list[str]], k: int = 60) -> list[str]:
    """Fuse several ID rankings with Reciprocal Rank Fusion.

    Each ranking contributes ``1 / (k + rank)`` per ID. The standard ``k=60`` avoids
    normalizing incomparable score scales such as BM25 and cosine distance.
    """
    score: dict[str, float] = {}
    for ranking in rankings:
        for rank, rid in enumerate(ranking, start=1):
            if not rid:
                continue
            score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank)
    return [rid for rid, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)]
