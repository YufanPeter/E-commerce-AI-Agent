from __future__ import annotations

"""Runtime Chroma retriever shared by API services.

Long-running services should create one ``ChromaRetriever`` at startup and reuse it so
the Chroma client and embedding connection remain warm.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag.chroma_store import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_PERSIST_DIR,
    create_collection,
)


@dataclass(frozen=True)
class RetrievedChunk:
    """Normalized Chroma hit used by downstream ranking and aggregation."""

    chunk_id: str
    document: str
    metadata: dict[str, Any]
    distance: float | None

    @property
    def product_id(self) -> str:
        return str(self.metadata.get("product_id", ""))

    @property
    def chunk_type(self) -> str:
        return str(self.metadata.get("chunk_type", ""))


class ChromaRetriever:
    """Lightweight runtime wrapper around the product knowledge collection."""

    def __init__(
        self,
        persist_dir: Path = DEFAULT_PERSIST_DIR,
        collection_name: str = DEFAULT_COLLECTION,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        """Load the collection and embedding function for repeated retrieval."""
        self.collection = create_collection(
            persist_dir=persist_dir,
            collection_name=collection_name,
            embedding_model=embedding_model,
            reset=False,
        )

    def count(self) -> int:
        """Return the current number of chunks in the collection."""
        return self.collection.count()

    def search(
        self,
        query: str,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """Retrieve semantic evidence chunks for a user query.

        ``where`` is a Chroma metadata prefilter for category or product ID. Hard business
        constraints such as price, SKU, and brand exclusion are enforced by ProductStore.
        """
        result = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return self._parse_result(result)

    def _parse_result(self, result: dict[str, Any]) -> list[RetrievedChunk]:
        """Convert Chroma's nested response into normalized result objects."""
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]

        chunks: list[RetrievedChunk] = []
        for index, chunk_id in enumerate(ids):
            distance = distances[index] if index < len(distances) else None
            chunks.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    document=documents[index],
                    metadata=metadatas[index],
                    distance=distance,
                )
            )
        return chunks
