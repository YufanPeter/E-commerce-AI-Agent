from __future__ import annotations

"""VisualIndex 纯逻辑单测（不触网）：余弦打分、批量打分、top_k、空索引降级。"""

import math

from search.visual_index import VisualIndex, load_visual_index


def test_score_self_is_one():
    idx = VisualIndex({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    # 与自身向量的余弦应为 1（归一化后点积）
    assert math.isclose(idx.score([1.0, 0.0], "a"), 1.0, abs_tol=1e-6)
    # 正交向量余弦为 0
    assert math.isclose(idx.score([1.0, 0.0], "b"), 0.0, abs_tol=1e-6)


def test_score_unknown_product_returns_none():
    idx = VisualIndex({"a": [1.0, 0.0]})
    assert idx.score([1.0, 0.0], "missing") is None


def test_score_is_magnitude_invariant():
    # 余弦只看方向，不看模长：放大查询向量不改变分数
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
    # 索引文件不存在时返回空索引而不报错（视觉重排自动失效）
    idx = load_visual_index(tmp_path / "nope.json")
    assert len(idx) == 0
