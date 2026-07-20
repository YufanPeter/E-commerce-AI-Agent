from __future__ import annotations

"""VisualIndex 纯逻辑单测（不触网）：余弦打分、批量打分、top_k、空索引降级。"""

import math

from search.visual_index import VisualIndex, load_visual_index


def test_score_self_is_one():
    idx = VisualIndex({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    # A vector's cosine similarity with itself should be 1 after normalization.
    assert math.isclose(idx.score([1.0, 0.0], "a"), 1.0, abs_tol=1e-6)
    # Orthogonal vectors have zero cosine similarity.
    assert math.isclose(idx.score([1.0, 0.0], "b"), 0.0, abs_tol=1e-6)


def test_score_unknown_product_returns_none():
    idx = VisualIndex({"a": [1.0, 0.0]})
    assert idx.score([1.0, 0.0], "missing") is None


def test_score_is_magnitude_invariant():
    # Cosine similarity depends on direction, so scaling the query does not change scores.
    idx = VisualIndex({"a": [3.0, 4.0]})
    s1 = idx.score([3.0, 4.0], "a")
    s2 = idx.score([30.0, 40.0], "a")
    assert math.isclose(s1, s2, abs_tol=1e-6)
    assert math.isclose(s1, 1.0, abs_tol=1e-6)


def test_score_many_skips_missing():
    idx = VisualIndex({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    out = idx.score_many([1.0, 0.0], ["a", "b", "missing"])
    assert set(out.keys()) == {"a", "b"}
    assert math.isclose(out["a"], 1.0, abs_tol=1e-6)


def test_top_k_orders_by_similarity():
    idx = VisualIndex({
        "near": [1.0, 0.1],
        "mid": [0.7, 0.7],
        "far": [0.0, 1.0],
    })
    ranked = idx.top_k([1.0, 0.0], k=2)
    assert [pid for pid, _ in ranked] == ["near", "mid"]


def test_empty_index_is_graceful():
    idx = VisualIndex({})
    assert len(idx) == 0
    assert idx.score([1.0], "x") is None
    assert idx.score_many([1.0], ["x"]) == {}
    assert idx.top_k([1.0], k=5) == []


def test_load_missing_file_returns_empty_index(tmp_path):
    # A missing index file returns an empty index and disables visual reranking without raising.
    idx = load_visual_index(tmp_path / "nope.json")
    assert len(idx) == 0
