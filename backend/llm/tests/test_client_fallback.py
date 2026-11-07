from __future__ import annotations

from llm.client import (
    _FallbackChatCompletions,
    _FallbackRerankClient,
    _embed_with_ark,
)


class _FakeCompletions:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def test_chat_falls_back_to_ark_and_replaces_model():
    primary = _FakeCompletions(error=ConnectionError("primary down"))
    fallback = _FakeCompletions(result="ark response")
    client = _FallbackChatCompletions(primary, fallback, "ark-endpoint")

    result = client.create(model="qwen3-vl-flash", messages=[], temperature=0)

    assert result == "ark response"
    assert fallback.calls[0]["model"] == "ark-endpoint"
    assert fallback.calls[0]["temperature"] == 0


def test_stream_falls_back_only_before_first_primary_chunk():
    def broken_stream():
        raise ConnectionError("stream failed before first token")
        yield  # pragma: no cover

    primary = _FakeCompletions(result=broken_stream())
    fallback = _FakeCompletions(result=iter(["ark-1", "ark-2"]))
    client = _FallbackChatCompletions(primary, fallback, "ark-endpoint")

    assert list(client.create(model="qwen", messages=[], stream=True)) == [
        "ark-1",
        "ark-2",
    ]


class _FakeEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, path, *, body, cast_to):
        self.calls.append({"path": path, "body": body, "cast_to": cast_to})
        return {"data": {"embedding": [float(len(self.calls)), 0.0]}}


def test_ark_embedding_fallback_maps_text_and_image_inputs():
    client = _FakeEmbeddingClient()

    vectors = _embed_with_ark(
        client,
        "ark-embedding-endpoint",
        [{"text": "防晒"}, {"image": "https://example.com/p.png"}],
    )

    assert vectors == [[1.0, 0.0], [2.0, 0.0]]
    assert client.calls[0]["body"]["input"] == [
        {"type": "text", "text": "防晒"}
    ]
    assert client.calls[1]["body"]["input"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://example.com/p.png"},
        }
    ]


class _FakeResponse:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, *, json):
        self.calls.append({"url": url, "json": json})
        return self.response


def test_rerank_falls_back_to_zhipu_and_replaces_model():
    primary = _FakeHttpClient(
        _FakeResponse(error=ConnectionError("dashscope down"))
    )
    fallback_response = _FakeResponse()
    fallback = _FakeHttpClient(fallback_response)
    client = _FallbackRerankClient(
        primary,
        fallback,
        "https://zhipu.example/rerank",
        "rerank-pro",
    )

    response = client.post(
        "https://dashscope.example/reranks",
        json={
            "model": "qwen3-rerank",
            "query": "防晒",
            "documents": ["A", "B"],
            "top_n": 2,
        },
    )

    assert response is fallback_response
    assert fallback.calls[0]["url"] == "https://zhipu.example/rerank"
    assert fallback.calls[0]["json"]["model"] == "rerank-pro"
