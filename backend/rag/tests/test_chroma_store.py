from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import rag.build_chroma as build_chroma
import rag.chroma_store as chroma_store
from rag.chroma_store import OpenAICompatibleEmbeddingFunction


class _FakeEmbeddings:
    """OpenAI-compatible embeddings resource with deterministic vectors."""

    def __init__(self) -> None:
        self.inputs: list[list[str]] = []
        self.models: list[str] = []

    def create(self, *, model: str, input: list[str]):
        self.models.append(model)
        self.inputs.append(input)
        data = [
            SimpleNamespace(index=index, embedding=[float(len(text)), float(index)])
            for index, text in enumerate(input)
        ]
        return SimpleNamespace(model=model, data=list(reversed(data)))


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddings()


class _MalformedIndexEmbeddings(_FakeEmbeddings):
    def create(self, *, model: str, input: list[str]):
        response = super().create(model=model, input=input)
        for item in response.data:
            item.index = 0
        return response


def _use_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    embedding_class = OpenAICompatibleEmbeddingFunction

    def build_embedding(
        model: str,
        batch_size: int = 64,
        endpoint: str | None = None,
    ):
        return embedding_class(
            model=model,
            batch_size=batch_size,
            client=_FakeClient(),
            endpoint=endpoint,
        )

    monkeypatch.setattr(
        chroma_store,
        "OpenAICompatibleEmbeddingFunction",
        build_embedding,
    )


def test_embedding_function_batches_standard_text_requests_and_restores_input_order():
    client = _FakeClient()
    embedding = OpenAICompatibleEmbeddingFunction(
        model="embedding-3",
        batch_size=2,
        client=client,
    )

    vectors = embedding(["a", "four", "seven"])

    assert client.embeddings.inputs == [["a", "four"], ["seven"]]
    assert client.embeddings.models == ["embedding-3", "embedding-3"]
    assert vectors == [[1.0, 0.0], [4.0, 1.0], [5.0, 0.0]]


def test_embedding_function_uses_identical_text_path_for_documents_and_queries():
    client = _FakeClient()
    embedding = OpenAICompatibleEmbeddingFunction(
        model="embedding-3",
        batch_size=64,
        client=client,
    )

    document_vectors = embedding.embed_documents(["商品文档"])
    query_vectors = embedding.embed_query(["用户查询"])

    assert document_vectors == [[4.0, 0.0]]
    assert query_vectors == [[4.0, 0.0]]
    assert client.embeddings.inputs == [["商品文档"], ["用户查询"]]


def test_embedding_function_skips_provider_for_empty_input():
    client = _FakeClient()
    embedding = OpenAICompatibleEmbeddingFunction(
        model="embedding-3",
        batch_size=64,
        client=client,
    )

    assert embedding([]) == []
    assert client.embeddings.inputs == []


def test_embedding_function_caps_provider_batches_at_sixty_four_inputs():
    client = _FakeClient()
    embedding = OpenAICompatibleEmbeddingFunction(
        model="embedding-3",
        batch_size=100,
        client=client,
    )

    embedding([str(index) for index in range(65)])

    assert [len(batch) for batch in client.embeddings.inputs] == [64, 1]


def test_embedding_function_rejects_malformed_response_indexes():
    client = _FakeClient()
    client.embeddings = _MalformedIndexEmbeddings()
    embedding = OpenAICompatibleEmbeddingFunction(
        model="embedding-3",
        batch_size=64,
        client=client,
    )

    with pytest.raises(ValueError, match="indexes"):
        embedding(["first", "second"])


def test_collection_persists_embedding_model_contract(tmp_path: Path, monkeypatch):
    _use_fake_provider(monkeypatch)

    collection = chroma_store.create_collection(
        persist_dir=tmp_path,
        collection_name="contract",
        embedding_model="model-a",
        reset=True,
    )

    assert collection.metadata["embedding_provider"] == "openai_compatible_text"
    assert collection.metadata["embedding_model"] == "model-a"


def test_collection_rejects_embedding_model_mismatch(tmp_path: Path, monkeypatch):
    _use_fake_provider(monkeypatch)
    chroma_store.create_collection(
        persist_dir=tmp_path,
        collection_name="contract",
        embedding_model="model-a",
        reset=True,
    )

    with pytest.raises(RuntimeError, match="embedding model"):
        chroma_store.create_collection(
            persist_dir=tmp_path,
            collection_name="contract",
            embedding_model="model-b",
        )


def test_collection_rejects_embedding_endpoint_mismatch(tmp_path: Path, monkeypatch):
    _use_fake_provider(monkeypatch)
    monkeypatch.setenv("ARK_EMBEDDING_BASE_URL", "https://provider-a.example/v1/")
    chroma_store.create_collection(
        persist_dir=tmp_path,
        collection_name="contract",
        embedding_model="model-a",
        reset=True,
    )
    monkeypatch.setenv("ARK_EMBEDDING_BASE_URL", "https://provider-b.example/v1/")

    with pytest.raises(RuntimeError, match="embedding endpoint mismatch"):
        chroma_store.create_collection(
            persist_dir=tmp_path,
            collection_name="contract",
            embedding_model="model-a",
        )


def test_collection_records_and_validates_stored_dimensions(tmp_path: Path, monkeypatch):
    _use_fake_provider(monkeypatch)
    collection = chroma_store.create_collection(
        persist_dir=tmp_path,
        collection_name="contract",
        embedding_model="model-a",
        reset=True,
    )
    chunks = [
        build_chroma.Chunk(
            id="one",
            document="one",
            metadata={"product_id": "one"},
        )
    ]
    build_chroma.upsert_chunks(collection, chunks, batch_size=64)

    dimensions = build_chroma.persist_collection_dimensions(collection)

    assert dimensions == 2
    assert collection.metadata["embedding_dimensions"] == 2

    collection.modify(
        metadata={**collection.metadata, "embedding_dimensions": 3},
    )
    with pytest.raises(RuntimeError, match="dimension"):
        chroma_store.create_collection(
            persist_dir=tmp_path,
            collection_name="contract",
            embedding_model="model-a",
        )


def test_runtime_chroma_defaults_follow_environment(monkeypatch):
    monkeypatch.setenv(
        "CHROMA_PERSIST_DIR",
        "backend/storage/chroma_phase_a_embedding3",
    )
    monkeypatch.setenv(
        "CHROMA_COLLECTION",
        "product_knowledge_embedding3_phase_a",
    )

    assert chroma_store._configured_persist_dir() == (
        chroma_store.PROJECT_ROOT / "backend/storage/chroma_phase_a_embedding3"
    )
    assert (
        chroma_store._configured_collection_name()
        == "product_knowledge_embedding3_phase_a"
    )
