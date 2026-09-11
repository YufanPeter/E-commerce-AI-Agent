from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from rag.chroma_store import DoubaoEmbeddingFunction


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


def test_embedding_function_batches_standard_text_requests_and_restores_input_order():
    client = _FakeClient()
    embedding = DoubaoEmbeddingFunction(
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
    embedding = DoubaoEmbeddingFunction(
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
    embedding = DoubaoEmbeddingFunction(
        model="embedding-3",
        batch_size=64,
        client=client,
    )

    assert embedding([]) == []
    assert client.embeddings.inputs == []


def test_embedding_function_caps_provider_batches_at_sixty_four_inputs():
    client = _FakeClient()
    embedding = DoubaoEmbeddingFunction(
        model="embedding-3",
        batch_size=100,
        client=client,
    )

    embedding([str(index) for index in range(65)])

    assert [len(batch) for batch in client.embeddings.inputs] == [64, 1]
