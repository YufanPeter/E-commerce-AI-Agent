"""Backend-neutral vector index boundary.

Callers express filters as ``Condition`` lists. Chroma's ``where`` syntax stays in this
module, so moving to another vector store needs one new adapter and a rebuilt index.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from rag.chroma_store import DEFAULT_EMBEDDING_MODEL, DashScopeEmbeddingFunction
from rag.retriever import ChromaRetriever, RetrievedChunk


@dataclass(frozen=True)
class Condition:
    """One metadata constraint; a list of conditions is a conjunction."""

    field: str
    op: str  # "eq" | "in" | "nin" | "gte" | "lte"
    value: Any


class VectorIndex(Protocol):
    def upsert(
        self,
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]],
        embeddings: Sequence[Sequence[float]] | None = None,
    ) -> None: ...

    def existing_ids(self) -> set[str]: ...

    def search(
        self, query: str, top_k: int, filters: Sequence[Condition] = (),
    ) -> list[RetrievedChunk]: ...

    def count(self) -> int: ...


def to_chroma_where(conditions: Sequence[Condition]) -> dict[str, Any] | None:
    """Translate conditions; Chroma rejects empty filters and one-item ``$and`` lists."""
    clauses = [
        {c.field: c.value} if c.op == "eq" else {c.field: {f"${c.op}": c.value}}
        for c in conditions
    ]
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class ChromaVectorIndex:
    """``VectorIndex`` over one persistent Chroma collection."""

    def __init__(
        self,
        persist_dir: Path,
        collection_name: str,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self._retriever = ChromaRetriever(persist_dir, collection_name, embedding_model)
        self._embedder = DashScopeEmbeddingFunction(model=embedding_model)

    def embed(self, documents: Sequence[str]) -> list[list[float]]:
        """Compute vectors outside the write path so a build can embed concurrently."""
        return self._embedder.embed_documents(documents)

    def upsert(
        self,
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]],
        embeddings: Sequence[Sequence[float]] | None = None,
    ) -> None:
        self._retriever.collection.upsert(
            ids=list(ids),
            documents=list(documents),
            metadatas=list(metadatas),
            embeddings=None if embeddings is None else [list(vector) for vector in embeddings],
        )

    def existing_ids(self) -> set[str]:
        return set(self._retriever.collection.get(include=[])["ids"])

    def search(
        self, query: str, top_k: int, filters: Sequence[Condition] = (),
    ) -> list[RetrievedChunk]:
        return self._retriever.search(query, top_k=top_k, where=to_chroma_where(filters))

    def count(self) -> int:
        return self._retriever.count()
