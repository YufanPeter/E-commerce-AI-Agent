from __future__ import annotations

"""Thin wrapper around an OpenAI-compatible Doubao Ark client.

Ark uses the OpenAI SDK protocol. This module loads provider configuration from the
environment and exposes process-wide clients to reuse HTTP connections.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from dotenv import load_dotenv

if TYPE_CHECKING:
    from openai import OpenAI

DEFAULT_RERANK_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/rerank"


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_env_once() -> None:
    """Load repository-root ``.env`` values without overriding the environment."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """Return the process-wide chat client, creating its connection pool lazily."""
    from openai import OpenAI

    _load_env_once()
    api_key = os.getenv("ARK_API_KEY")
    base_url = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3/")
    if not api_key:
        raise RuntimeError(
            "ARK_API_KEY is missing. Create a root .env file and provide a compatible chat API key."
        )
    # Bound the SDK timeout so a stalled provider cannot block the retrieval chain.
    timeout = float(os.getenv("ARK_TIMEOUT", "30"))
    max_retries = int(os.getenv("ARK_MAX_RETRIES", "1"))
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def get_model_id() -> str:
    """Return the configured default model or endpoint ID."""
    _load_env_once()
    model = os.getenv("ARK_MODEL")
    if not model:
        raise RuntimeError("ARK_MODEL is missing. Set the general model or endpoint ID in .env.")
    return model


@lru_cache(maxsize=1)
def get_embedding_client() -> OpenAI:
    """Return the process-wide embedding client.

    Embeddings may use a separate API key and base URL. Keys fall back from
    ``ARK_EMBEDDING_API_KEY`` to ``ARK_API_KEY``; base URLs fall back from
    ``ARK_EMBEDDING_BASE_URL`` to ``ARK_BASE_URL`` and then the default Ark endpoint.
    """
    from openai import OpenAI

    _load_env_once()
    api_key = os.getenv("ARK_EMBEDDING_API_KEY") or os.getenv("ARK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "The embedding API key is missing. Set ARK_EMBEDDING_API_KEY in .env "
            "or reuse ARK_API_KEY."
        )
    base_url = (
        os.getenv("ARK_EMBEDDING_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or "https://ark.cn-beijing.volces.com/api/v3/"
    )
    timeout = float(os.getenv("ARK_TIMEOUT", "30"))
    max_retries = int(os.getenv("ARK_MAX_RETRIES", "1"))
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


@lru_cache(maxsize=1)
def get_rerank_client() -> httpx.Client:
    """Return the process-wide reranking HTTP client.

    Cloud reranking avoids large local inference dependencies such as PyTorch and
    sentence-transformers. The configured service uses bearer authentication.

    Only ``ZHIPU_API_KEY`` is accepted to prevent accidental cross-provider credentials.
    """
    _load_env_once()
    api_key = os.getenv("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError(
            "The reranking API key is missing. Set ZHIPU_API_KEY in .env, "
            "or set USE_RERANK=0 when reranking is not required."
        )
    timeout = float(os.getenv("RERANK_TIMEOUT", os.getenv("ARK_TIMEOUT", "30")))
    return httpx.Client(
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )


def get_rerank_base_url() -> str:
    """Return the reranking endpoint URL."""
    _load_env_once()
    return os.getenv("RERANK_BASE_URL", DEFAULT_RERANK_BASE_URL)


def get_rerank_model_id() -> str:
    """Return the configured reranking model name."""
    _load_env_once()
    model = os.getenv("RERANK_MODEL")
    if not model:
        raise RuntimeError("RERANK_MODEL is missing. Set the reranking model ID in .env.")
    return model
