"""Shared evaluation utilities for path setup, datasets, metrics, and result formatting.

Keeping them here avoids duplicating sys.path, dotenv, and metric implementations across scripts.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Iterable

# Add the backend root to sys.path so imports such as `from search.xxx import` work,
# matching the bootstrap used by tests under backend.
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"


def load_env() -> None:
    """Load the project-root `.env` used by `llm/client.py`."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # dotenv is optional when the shell already provides environment variables.
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def load_dataset(name: str) -> dict[str, Any]:
    """Read an annotated JSON file from `datasets/`."""
    path = DATASETS_DIR / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def install_requests_embedding_fallback() -> bool:
    """Route outbound httpx traffic through system curl to bypass local Python TLS failures.

    Some local Python and OpenSSL combinations repeatedly fail the TLS handshake with the Ark
    endpoint while system curl remains stable. A custom httpx transport forwards requests to a
    curl subprocess and wraps the result in an httpx response. Request construction, response
    parsing, endpoints, models, and authentication remain unchanged for chat, embedding, and
    reranking clients. Returns whether the fallback was installed successfully.
    """
    try:
        import shutil

        import httpx

        from llm import client as llm_client
    except Exception:
        return False

    if shutil.which("curl") is None:
        return False

    import subprocess

    class _CurlTransport(httpx.BaseTransport):
        """Forward one httpx request through curl to bypass Python TLS handshake issues."""

        def handle_request(self, request: "httpx.Request") -> "httpx.Response":
            content = request.read()
            cmd = [
                "curl", "-sS", "--compressed", "--max-time", "60",
                "-X", request.method, str(request.url), "-w", "\n%{http_code}",
            ]
            for key, value in request.headers.items():
                if key.lower() in ("host", "content-length", "accept-encoding", "connection"):
                    continue
                cmd += ["-H", f"{key}: {value}"]
            if content:
                cmd += ["--data-binary", "@-"]
            proc = subprocess.run(cmd, input=content, capture_output=True)
            out = proc.stdout or b""
            idx = out.rfind(b"\n")
            if idx < 0:
                return httpx.Response(status_code=502, content=proc.stderr or b"curl failed", request=request)
            body, status_raw = out[:idx], out[idx + 1:]
            try:
                status = int(status_raw)
            except ValueError:
                status = 502
            return httpx.Response(
                status_code=status,
                headers={"content-type": "application/json"},
                content=body,
                request=request,
            )

    def _wrap_openai(get_client_fn: Any) -> None:
        client = get_client_fn()
        inner = client._client  # type: ignore[attr-defined]
        client._client = httpx.Client(  # type: ignore[attr-defined]
            transport=_CurlTransport(),
            base_url=inner.base_url,
            timeout=60,
        )

    # Chat and embedding both use OpenAI clients; replace their internal httpx clients.
    try:
        _wrap_openai(llm_client.get_client)
    except Exception:
        pass
    try:
        _wrap_openai(llm_client.get_embedding_client)
    except Exception:
        pass

    # Reranking uses a plain httpx client; clear its cache and install a curl-backed singleton.
    try:
        _orig_get_rerank = llm_client.get_rerank_client
        try:
            _orig_get_rerank.cache_clear()  # type: ignore[attr-defined]
        except Exception:
            pass
        api_key = os.getenv("ZHIPU_API_KEY")
        rerank_client = httpx.Client(
            transport=_CurlTransport(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        llm_client.get_rerank_client = lambda: rerank_client  # type: ignore[assignment]
    except Exception:
        pass

    return True


# Descriptive alias: the fallback routes outbound httpx traffic through curl.
install_curl_transport_fallback = install_requests_embedding_fallback





# ---------------------------------------------------------------------------
# Retrieval metrics: Recall@K, MRR, and NDCG@K.
# Each metric scores a ranked prediction list against the annotated relevant-ID set.
# ---------------------------------------------------------------------------


def recall_at_k(predicted: list[str], relevant: set[str], k: int) -> float:
    """Return relevant products found in the top K divided by all relevant products."""
    if not relevant:
        return 0.0
    topk = predicted[:k]
    hit = sum(1 for pid in topk if pid in relevant)
    return hit / len(relevant)


def reciprocal_rank(predicted: list[str], relevant: set[str]) -> float:
    """Return the reciprocal rank of the first relevant hit, or zero when none is found."""
    for idx, pid in enumerate(predicted, start=1):
        if pid in relevant:
            return 1.0 / idx
    return 0.0


def ndcg_at_k(predicted: list[str], relevant: set[str], k: int) -> float:
    """Return NDCG@K using binary relevance."""
    if not relevant:
        return 0.0
    dcg = 0.0
    for idx, pid in enumerate(predicted[:k], start=1):
        if pid in relevant:
            dcg += 1.0 / math.log2(idx + 1)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def hr(char: str = "=", width: int = 78) -> str:
    return char * width
