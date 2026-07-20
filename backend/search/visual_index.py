from __future__ import annotations

"""Offline product-image vectors with online visual-similarity scoring.

``build_image_index`` writes product vectors to ``backend/storage/image_index.json``.
At runtime, ``VisualIndex`` loads them once and calculates cosine similarity for the
candidate set returned by text retrieval.

The small catalog does not require a separate ANN service. Vectors are normalized at
load time so cosine similarity becomes a fast in-memory dot product.
"""

import json
import logging
import math
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX_PATH = PROJECT_ROOT / "backend" / "storage" / "image_index.json"


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


class VisualIndex:
    """In-memory product-image vector index with cosine scoring."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        # Normalize once so online cosine calculations reduce to dot products.
        self._vectors: dict[str, list[float]] = {
            pid: _normalize(vec) for pid, vec in vectors.items()
        }

    def __len__(self) -> int:
        return len(self._vectors)

    def has(self, product_id: str) -> bool:
        return product_id in self._vectors

    def score(self, query_vec: list[float], product_id: str) -> float | None:
        """Return cosine similarity for one product, or ``None`` when unindexed."""
        target = self._vectors.get(product_id)
        if target is None:
            return None
        q = _normalize(query_vec)
        return sum(a * b for a, b in zip(q, target))

    def score_many(
        self, query_vec: list[float], product_ids: list[str]
    ) -> dict[str, float]:
        """Score candidates and omit products without indexed vectors."""
        q = _normalize(query_vec)
        out: dict[str, float] = {}
        for pid in product_ids:
            target = self._vectors.get(pid)
            if target is not None:
                out[pid] = sum(a * b for a, b in zip(q, target))
        return out

    def top_k(self, query_vec: list[float], k: int = 10) -> list[tuple[str, float]]:
        """Return full-catalog visual neighbors using a small brute-force scan."""
        q = _normalize(query_vec)
        scored = [
            (pid, sum(a * b for a, b in zip(q, vec)))
            for pid, vec in self._vectors.items()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]


def load_visual_index(path: Path = DEFAULT_INDEX_PATH) -> VisualIndex:
    """Load the visual index, returning an empty index when the file is absent."""
    if not path.exists():
        logger.warning(
            "Visual index %s is missing; visual reranking is disabled until build_image_index runs.",
            path,
        )
        return VisualIndex({})
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info("Loaded %d product-image vectors from the visual index", len(data))
    return VisualIndex(data)


@lru_cache(maxsize=1)
def get_visual_index() -> VisualIndex:
    """Return the process-wide visual index used for reranking."""
    return load_visual_index()
