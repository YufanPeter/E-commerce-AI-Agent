from __future__ import annotations

"""Shared Chroma connection and collection initialization."""

import os
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Load ``.env`` before evaluating the module-level embedding-model setting.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _configured_persist_dir() -> Path:
    value = os.getenv("CHROMA_PERSIST_DIR")
    if not value:
        return PROJECT_ROOT / "backend" / "storage" / "chroma"
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _configured_collection_name() -> str:
    return os.getenv("CHROMA_COLLECTION", "product_knowledge")


def _configured_embedding_endpoint() -> str:
    value = (
        os.getenv("ARK_EMBEDDING_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or "https://ark.cn-beijing.volces.com/api/v3/"
    )
    return value.rstrip("/") + "/"


DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PERSIST_DIR = _configured_persist_dir()
DEFAULT_COLLECTION = _configured_collection_name()
# Use a cloud embedding API by default; configure its model in ``ARK_EMBEDDING_MODEL``.
DEFAULT_EMBEDDING_MODEL = os.getenv("ARK_EMBEDDING_MODEL", "doubao-embedding-text-240715")

# Maximum provider text batch size used to respect API limits.
_EMBEDDING_BATCH_SIZE = int(os.getenv("ARK_EMBEDDING_BATCH_SIZE", "64"))
_MAX_PROVIDER_BATCH_SIZE = 64
_EMBEDDING_PROVIDER = "openai_compatible_text"


class OpenAICompatibleEmbeddingFunction:
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
        endpoint: str | None = None,
    ) -> None:
        self._model = model
        self._batch_size = min(max(1, batch_size), _MAX_PROVIDER_BATCH_SIZE)
        self._client = client
        self._endpoint = (endpoint or _configured_embedding_endpoint()).rstrip("/") + "/"

    def _get_client(self) -> Any:
        if self._client is None:
            # Credentials and the base URL are resolved only when inference is needed.
            from llm.client import get_embedding_client

            self._client = get_embedding_client()
        return self._client

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed one ordered text batch through the standard endpoint."""
        response = self._get_client().embeddings.create(
            model=self._model,
            input=texts,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        indexes = [item.index for item in ordered]
        if indexes != list(range(len(texts))):
            raise ValueError(
                "Embedding response indexes must contain each input position exactly once"
            )
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

    @staticmethod
    def name() -> str:
        """Return the embedding-function identifier persisted by Chroma."""
        return _EMBEDDING_PROVIDER

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "OpenAICompatibleEmbeddingFunction":
        """Recreate the adapter from Chroma's non-secret persisted config."""
        return OpenAICompatibleEmbeddingFunction(
            model=str(config["model"]),
            batch_size=int(config.get("batch_size", _EMBEDDING_BATCH_SIZE)),
            endpoint=str(config["endpoint"]),
        )

    def get_config(self) -> dict[str, Any]:
        """Persist model identity and batching without storing credentials."""
        return {
            "model": self._model,
            "batch_size": self._batch_size,
            "endpoint": self._endpoint,
        }

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        if not config.get("model"):
            raise ValueError("Embedding model must be recorded in the Chroma config")
        if not config.get("endpoint"):
            raise ValueError("Embedding endpoint must be recorded in the Chroma config")

    def validate_config_update(
        self,
        old_config: dict[str, Any],
        new_config: dict[str, Any],
    ) -> None:
        if old_config.get("model") != new_config.get("model"):
            raise ValueError(
                "Cannot change the embedding model of an existing Chroma collection"
            )
        if old_config.get("endpoint") != new_config.get("endpoint"):
            raise ValueError(
                "Cannot change the embedding endpoint of an existing Chroma collection"
            )


# Preserve the upstream public name for callers that imported it before the text adapter.
DoubaoEmbeddingFunction = OpenAICompatibleEmbeddingFunction


def _validate_collection_contract(
    collection: Any,
    embedding_model: str,
    embedding_endpoint: str,
) -> None:
    metadata = collection.metadata or {}
    if metadata.get("embedding_provider") != _EMBEDDING_PROVIDER:
        raise RuntimeError(
            f"Chroma collection {collection.name!r} has no compatible embedding provider contract"
        )
    if metadata.get("embedding_model") != embedding_model:
        raise RuntimeError(
            f"Chroma collection {collection.name!r} embedding model mismatch: "
            f"stored={metadata.get('embedding_model')!r}, requested={embedding_model!r}"
        )
    if metadata.get("embedding_endpoint") != embedding_endpoint:
        raise RuntimeError(
            f"Chroma collection {collection.name!r} embedding endpoint mismatch: "
            f"stored={metadata.get('embedding_endpoint')!r}, "
            f"requested={embedding_endpoint!r}"
        )

    stored_dimensions = metadata.get("embedding_dimensions")
    if stored_dimensions is None or collection.count() == 0:
        return
    sample = collection.peek(limit=1).get("embeddings")
    actual_dimensions = len(sample[0]) if sample is not None and len(sample) else None
    if actual_dimensions != int(stored_dimensions):
        raise RuntimeError(
            f"Chroma collection {collection.name!r} dimension mismatch: "
            f"stored={stored_dimensions!r}, actual={actual_dimensions!r}"
        )


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
    embedding_function = OpenAICompatibleEmbeddingFunction(model=embedding_model)
    try:
        collection = client.get_collection(
            name=collection_name,
            embedding_function=embedding_function,
        )
    except chromadb.errors.NotFoundError:
        return client.create_collection(
            name=collection_name,
            embedding_function=embedding_function,
            metadata={
                "embedding_provider": _EMBEDDING_PROVIDER,
                "embedding_model": embedding_model,
                "embedding_endpoint": embedding_function._endpoint,
            },
            configuration={"hnsw": {"space": "cosine"}},
        )
    except ValueError as exc:
        raise RuntimeError(
            f"Chroma collection {collection_name!r} embedding model/config mismatch"
        ) from exc

    _validate_collection_contract(
        collection,
        embedding_model,
        embedding_function._endpoint,
    )
    return collection
