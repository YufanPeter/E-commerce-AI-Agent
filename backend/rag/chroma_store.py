from __future__ import annotations

"""Shared Chroma connection and collection initialization."""

import os
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Load ``.env`` before evaluating the module-level embedding-model setting.
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PERSIST_DIR = PROJECT_ROOT / "backend" / "storage" / "chroma"
DEFAULT_COLLECTION = "product_knowledge"
# Use a cloud embedding API by default; configure its model in ``ARK_EMBEDDING_MODEL``.
DEFAULT_EMBEDDING_MODEL = os.getenv("ARK_EMBEDDING_MODEL", "doubao-embedding-text-240715")

# Maximum concurrent text embeddings used to respect provider limits.
_EMBEDDING_BATCH_SIZE = int(os.getenv("ARK_EMBEDDING_BATCH_SIZE", "64"))
_MAX_PROVIDER_BATCH_SIZE = 64


class DoubaoEmbeddingFunction:
    """Chroma embedding function backed by an OpenAI-compatible text API.

    Remote inference removes local PyTorch/sentence-transformers dependencies. Offline
    indexing and online retrieval share the same model to keep vector spaces consistent.

    The standard ``/embeddings`` endpoint accepts text batches. Rebuild the vector
    collection whenever the embedding model or output dimension changes.
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBEDDING_MODEL,
        batch_size: int = _EMBEDDING_BATCH_SIZE,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._batch_size = min(max(1, batch_size), _MAX_PROVIDER_BATCH_SIZE)
        if client is None:
            # Use the dedicated embedding client because credentials and base URL may differ.
            from llm.client import get_embedding_client

            client = get_embedding_client()
        self._client = client

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed one ordered text batch through the standard endpoint."""
        response = self._client.embeddings.create(
            model=self._model,
            input=texts,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]

    # Chroma validates that this parameter is named ``input``.
    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        texts = [text if isinstance(text, str) else str(text) for text in input]
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self._batch_size]))
        return vectors

    # Delegate query and document methods to the same implementation across Chroma versions.
    def embed_query(self, input: Sequence[str]) -> list[list[float]]:
        return self.__call__(input)

    def embed_documents(self, input: Sequence[str]) -> list[list[float]]:
        return self.__call__(input)

    def name(self) -> str:
        """Return the embedding-function identifier persisted by Chroma."""
        return "doubao_ark"


def create_collection(
    persist_dir: Path = DEFAULT_PERSIST_DIR,
    collection_name: str = DEFAULT_COLLECTION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    reset: bool = False,
):
    """Open the configured Chroma collection, optionally clearing its old index.

    Offline indexing and runtime retrieval share this factory to keep collection name,
    persistence path, embedding model, and distance metric consistent.
    """
    try:
        import chromadb
    except ImportError as exc:
        raise SystemExit(
            "缺少依赖。请在仓库根目录运行："
            "source .venv/bin/activate && pip install -r backend/requirements.txt"
        ) from exc

    persist_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(persist_dir),
        settings=chromadb.Settings(anonymized_telemetry=False),
    )
    if reset:
        try:
            client.delete_collection(collection_name)
        except chromadb.errors.NotFoundError:
            pass

    # Chroma uses the same function for offline chunks and online queries.
    embedding_function = DoubaoEmbeddingFunction(model=embedding_model)
    return client.get_or_create_collection(
        name=collection_name,
        embedding_function=embedding_function,
        metadata={"hnsw:space": "cosine"},
    )
