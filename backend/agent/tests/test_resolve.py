from __future__ import annotations

"""Unit tests for the shared layered resolvers `resolve_one` and `resolve_many`.

Deterministic ordinal and unique-name paths must avoid the LLM, ambiguous cases may use it,
unavailable LLM calls must degrade safely, and multi-resolution must merge then truncate to K.
Every LLM call is monkeypatched, so the tests are offline and deterministic.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import agent.tools.resolve as rv
from agent.tools.resolve import resolve_one, resolve_many


_HITS = [
    {"product_id": "p1", "title": "华为 FreeBuds Pro 3 真无线耳机", "sub_category": "真无线耳机"},
    {"product_id": "p2", "title": "华为 Pura 90 影像手机", "sub_category": "智能手机"},
    {"product_id": "p3", "title": "小米 15 Ultra 手机", "sub_category": "智能手机"},
]


def _no_llm(monkeypatch):
    """Disable the LLM so any path reaching it returns empty for fallback assertions."""
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)
    monkeypatch.setattr(rv, "llm_pick_candidates", lambda *a, **k: [])


# --------------------------- resolve_one ---------------------------

def test_resolve_one_ordinal_no_llm(monkeypatch):
    # A unique ordinal match should return directly without calling the LLM.
    called = {"n": 0}
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert resolve_one("第二个", _HITS) == 1
    assert called["n"] == 0


def test_resolve_one_unique_name_no_llm(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    # Only p3 contains the named brand, producing a unique name match.
    assert resolve_one("小米那款", _HITS) == 2
    assert called["n"] == 0


def test_resolve_one_ambiguous_falls_to_llm(monkeypatch):
    # The shared brand is ambiguous, so the LLM selects p1 by subcategory.
    def fake_pick(query, candidates, timeout=5.0):
        for i, c in enumerate(candidates):
            if "真无线耳机" in str(c.get("sub_category", "")):
                return i
        return None

    monkeypatch.setattr(rv, "llm_pick_candidate", fake_pick)
    assert resolve_one("华为耳机", _HITS) == 0


def test_resolve_one_llm_unavailable_returns_none(monkeypatch):
    _no_llm(monkeypatch)
    # Ambiguity with no LLM returns None so the caller can clarify.
    assert resolve_one("华为", _HITS) is None


def test_resolve_one_empty_candidates():
    assert resolve_one("华为耳机", []) is None


# --------------------------- resolve_many ---------------------------

def test_resolve_many_two_ordinals_no_llm(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(rv, "llm_pick_candidates", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])
    assert resolve_many("第一个和第三个", _HITS, k=2) == [0, 2]
    assert called["n"] == 0


def test_resolve_many_merges_and_truncates(monkeypatch):
    _no_llm(monkeypatch)
    # Merge the name match for p3 with the ordinal match for p1, then truncate to K.
    assert resolve_many("第一个和小米", _HITS, k=2) == [0, 2]


def test_resolve_many_llm_fallback_when_short(monkeypatch):
    # When rules find only one product, let the LLM complete the pair.
    def fake_many(query, candidates, k=2, timeout=5.0):
        return [0, 1]

    monkeypatch.setattr(rv, "llm_pick_candidates", fake_many)
    out = resolve_many("华为这两款", _HITS, k=2)
    assert out[:2] == [0, 1]


def test_resolve_many_k_zero():
    assert resolve_many("第一个和第二个", _HITS, k=0) == []
