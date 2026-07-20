from __future__ import annotations

"""Offline logic tests for CompareTool and the comparison builder with a mocked LLM.

Covers reference resolution, explicit IDs, deterministic price rows, and dimension alignment,
deduplication, and fallbacks.
"""

from unittest.mock import patch

from agent.session import AgentSession
from agent.tools.compare import CompareTool
from store.product_store import PriceRange, ProductDetail


def _detail(pid, title, brand, category, low, high):
    return ProductDetail(
        product_id=pid,
        title=title,
        brand=brand,
        category=category,
        sub_category=None,
        base_price=low,
        image_path=None,
        image_url=f"http://img/{pid}.jpg",
        price_range=PriceRange(min_price=low, max_price=high, sku_count=2),
        skus=[],
        marketing_description=f"{title} 的卖点描述",
        faqs=[],
        reviews=[],
    )


class _FakeStore:
    def __init__(self, details):
        self._by_id = {d.product_id: d for d in details}

    def get_product_detail(self, pid):
        return self._by_id.get(pid)


def _session_with_hits(hits):
    sess = AgentSession(session_id="t")
    sess.set("last_hits", hits)
    return sess


_HITS = [
    {"product_id": "p1", "title": "Apple iPhone 17 Pro"},
    {"product_id": "p2", "title": "小米 17 Ultra"},
    {"product_id": "p3", "title": "华为 Mate 80"},
]

_DETAILS = [
    _detail("p1", "Apple iPhone 17 Pro", "Apple 苹果", "数码电子", 8999, 9999),
    _detail("p2", "小米 17 Ultra", "小米", "数码电子", 7499, 8499),
    _detail("p3", "华为 Mate 80", "华为", "数码电子", 6999, 7999),
]


def _fake_build(details, focus="", timeout=None):
    """Return a deterministic price row and mock dimension without calling the LLM."""
    from agent.comparison import _price_row, _product_header

    return {
        "title": "对比",
        "products": [_product_header(d) for d in details],
        "rows": [_price_row(details), {"label": "性能", "values": ["强"] * len(details), "highlight": 0}],
        "recommendation": "看需求选。",
    }


class TestResolveTargets:
    def _tool(self):
        return CompareTool(product_store=_FakeStore(_DETAILS))

    def test_explicit_ordinals(self):
        tool = self._tool()
        ids = tool._resolve_targets("对比第一个和第三个", _HITS)
        assert ids == ["p1", "p3"]

    def test_first_and_last(self):
        tool = self._tool()
        ids = tool._resolve_targets("对比一下第一个和最后一个", _HITS)
        assert ids == ["p1", "p3"]

    def test_brand_names(self):
        tool = self._tool()
        ids = tool._resolve_targets("对比小米和华为", _HITS)
        assert set(ids) == {"p2", "p3"}

    def test_named_products_win_over_generic_pair_words(self):
        """Generic pair wording must not override two explicitly named products."""
        tool = self._tool()
        ids = tool._resolve_targets("我想对比一下小米和华为这两款", _HITS)
        assert ids == ["p2", "p3"]

    def test_rewritten_names_do_not_match_generic_pro_tokens(self):
        """Do not match the first Apple product merely because a rewritten target contains `Pro`."""
        hits = [
            {"product_id": "a1", "title": "Apple iPhone 17 Pro"},
            {"product_id": "a2", "title": "Apple iPhone 17 Pro Max"},
            {"product_id": "xm", "title": "小米 MIX Fold 5 内折大屏旗舰折叠屏手机"},
            {"product_id": "hw", "title": "华为HUAWEI Pura 90 Pro 超感光影像曲面屏手机"},
        ]
        tool = self._tool()
        ids = tool._resolve_targets("对比推荐列表里的小米MIX Fold 5和华为Pura 90 Pro这两款手机的差异", hits)
        assert ids == ["xm", "hw"]

    def test_partial_name_match_does_not_default_to_first_two(self):
        """Do not default to the first two products when only one of two named targets is present."""
        hits = [
            {"product_id": "a1", "title": "Apple iPhone 17 Pro"},
            {"product_id": "a2", "title": "Apple iPhone 17 Pro Max"},
            {"product_id": "oppo", "title": "OPPO Find X9 Ultra"},
            {"product_id": "hw", "title": "华为HUAWEI Pura 90 Pro"},
        ]
        tool = self._tool()
        ids = tool._resolve_targets("对比小米和华为这两款", hits)
        assert ids == ["hw"]

    def test_default_first_two_when_vague(self):
        tool = self._tool()
        ids = tool._resolve_targets("对比一下", _HITS)
        assert ids == ["p1", "p2"]

    def test_generic_pair_words_still_default_when_no_names(self):
        tool = self._tool()
        ids = tool._resolve_targets("这两个有什么区别", _HITS)
        assert ids == ["p1", "p2"]

    def test_caps_at_three(self):
        tool = self._tool()
        ids = tool._resolve_targets("全部对比", _HITS)
        assert len(ids) <= 3


class TestCompareRun:
    def test_needs_at_least_two_hits(self):
        tool = CompareTool(product_store=_FakeStore(_DETAILS))
        sess = _session_with_hits([_HITS[0]])
        res = tool.run("对比", sess, {})
        assert res.payload["comparison"] is None
        assert "推荐" in res.narrative_override

    def test_explicit_product_ids_slot(self):
        tool = CompareTool(product_store=_FakeStore(_DETAILS))
        sess = _session_with_hits(_HITS)
        with patch("agent.tools.compare.build_comparison", side_effect=_fake_build):
            res = tool.run("", sess, {"product_ids": ["p2", "p3"]})
        comp = res.payload["comparison"]
        assert [p["product_id"] for p in comp["products"]] == ["p2", "p3"]

    def test_happy_path_writes_back_hits(self):
        tool = CompareTool(product_store=_FakeStore(_DETAILS))
        sess = _session_with_hits(_HITS)
        with patch("agent.tools.compare.build_comparison", side_effect=_fake_build):
            res = tool.run("对比第一个和第二个", sess, {})
        comp = res.payload["comparison"]
        assert [p["product_id"] for p in comp["products"]] == ["p1", "p2"]
        # Write compared products back to `last_hits` for subsequent references.
        assert [h["product_id"] for h in sess.recall_hits()] == ["p1", "p2"]


class TestPendingCompareClarification:
    """Persist a clarification when fewer than two products resolve, then resume on the next turn."""

    def test_ambiguous_sets_pending_compare(self):
        tool = CompareTool(product_store=_FakeStore(_DETAILS))
        # Resolving only one product should ask a clarification and store pending state.
        sess = _session_with_hits(_HITS)
        res = tool.run("对比华为", sess, {})
        assert res.payload["comparison"] is None
        assert sess.get("pending_compare") is not None

    def test_followup_ordinals_complete_compare(self):
        tool = CompareTool(product_store=_FakeStore(_DETAILS))
        sess = _session_with_hits(_HITS)
        sess.set("pending_compare", {"hit_count": 3})
        with patch("agent.tools.compare.build_comparison", side_effect=_fake_build):
            res = tool.run("第一个和第三个", sess, {})
        comp = res.payload["comparison"]
        assert [p["product_id"] for p in comp["products"]] == ["p1", "p3"]
        # Clear pending state after a successful comparison.
        assert sess.get("pending_compare") is None

    def test_selection_reply_detection(self):
        from agent.tools.compare import is_compare_selection_reply

        # Ordinals, conjunctions, and two named products are selection replies.
        assert is_compare_selection_reply("第一个和第三个", 3) is True
        assert is_compare_selection_reply("华为和小米", 3) is True
        assert is_compare_selection_reply("小米跟苹果", 3) is True
        # A new search or cart request is not a selection reply.
        assert is_compare_selection_reply("推荐几款平板", 3) is False
        assert is_compare_selection_reply("加入购物车", 3) is False
        assert is_compare_selection_reply("", 3) is False


class TestComparisonBuilder:
    def test_price_row_highlights_cheapest(self):
        from agent.comparison import _price_row

        row = _price_row(_DETAILS)
        assert row["label"] == "价格"
        assert row["highlight"] == 2  # p3 is the least expensive product.

    def test_build_comparison_requires_two(self):
        from agent.comparison import build_comparison

        try:
            build_comparison([_DETAILS[0]])
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_build_comparison_is_pure_lookup_no_llm(self):
        """Use a mock index without any LLM call; price is deterministic and text dimensions have no winner."""
        from agent import comparison

        index = comparison.CompareIndex({
            "p1": {"dims": {"性能/芯片": "A19 强", "续航": "中等", "屏幕": "XDR", "存储规格": "256G"}, "tagline": "性能党"},
            "p2": {"dims": {"性能/芯片": "澎湃", "续航": "更优", "屏幕": "2K高刷", "存储规格": "256G起"}, "tagline": "性价比党"},
        })
        with patch.object(comparison, "get_compare_index", return_value=index):
            result = comparison.build_comparison(_DETAILS[:2])

        labels = [r["label"] for r in result["rows"]]
        assert labels[0] == "价格"
        assert labels[1:] == comparison._CATEGORY_DIMENSIONS["数码电子"]
        # Highlight the cheapest price while leaving text dimensions without a winner.
        assert result["rows"][0]["highlight"] == 1  # p2(7499) < p1(8999)
        assert all(r["highlight"] is None for r in result["rows"][1:])
        # Dimension values come from the index.
        assert result["rows"][1]["values"] == ["A19 强", "澎湃"]
        # Purchase advice is disabled, so recommendation remains empty.
        assert result["recommendation"] == ""

    def test_missing_index_degrades_to_price_only(self):
        """Use placeholder values for all dimensions when the index is empty."""
        from agent import comparison

        with patch.object(comparison, "get_compare_index", return_value=comparison.CompareIndex({})):
            result = comparison.build_comparison(_DETAILS[:2])
        # Dimension rows remain present, but all values are placeholders.
        assert result["rows"][0]["label"] == "价格"
        for row in result["rows"][1:]:
            assert row["values"] == ["—", "—"]
        assert result["recommendation"] == ""


class TestFixedDimensions:
    """Use code-defined dimensions for stable, reproducible comparisons within a category."""

    def test_same_category_uses_category_dimensions(self):
        from agent.comparison import _dimensions_for, _CATEGORY_DIMENSIONS

        dims = _dimensions_for(_DETAILS[:2])  # Both products are electronics.
        assert dims == _CATEGORY_DIMENSIONS["数码电子"]

    def test_cross_category_falls_back_to_generic(self):
        from agent.comparison import _dimensions_for, _GENERIC_DIMENSIONS

        digital = _DETAILS[0]
        clothes = _detail("c9", "某T恤", "某牌", "服饰运动", 99, 199)
        assert _dimensions_for([digital, clothes]) == _GENERIC_DIMENSIONS

    def test_all_dimensions_for_category_includes_generic(self):
        """Include category-specific and generic dimensions for offline extraction and cross-category comparison."""
        from agent.comparison import all_dimensions_for_category, _CATEGORY_DIMENSIONS, _GENERIC_DIMENSIONS

        dims = all_dimensions_for_category("数码电子")
        for d in _CATEGORY_DIMENSIONS["数码电子"]:
            assert d in dims
        for d in _GENERIC_DIMENSIONS:
            assert d in dims



def test_run_uses_llm_fallback_when_rules_find_one(monkeypatch):
    """Use LLM fallback to complete a pair when deterministic rules resolve fewer than two products."""
    import agent.tools.compare as compare_module

    tool = CompareTool(product_store=_FakeStore(_DETAILS))
    monkeypatch.setattr(compare_module, "build_comparison", _fake_build)
    # Rules resolve one named product; the LLM fallback completes the pair with p2 and p3.
    monkeypatch.setattr(
        compare_module, "resolve_many", lambda query, cands, k=2: [1, 2]
    )
    sess = _session_with_hits(_HITS)
    res = tool.run("对比小米和另一个旗舰这两款", sess, {})
    # Produce a comparison instead of another clarification.
    assert res.payload["comparison"] is not None
    ids = [p["product_id"] for p in res.payload["comparison"]["products"]]
    assert ids == ["p2", "p3"]


def test_run_asks_when_llm_also_fails(monkeypatch):
    """Ask and store pending state when the LLM fallback also cannot resolve two products."""
    import agent.tools.compare as compare_module

    tool = CompareTool(product_store=_FakeStore(_DETAILS))
    monkeypatch.setattr(compare_module, "resolve_many", lambda query, cands, k=2: [])
    sess = _session_with_hits(_HITS)
    # Rules resolve one named product and the empty LLM fallback requires clarification.
    res = tool.run("对比小米这款和另一个", sess, {})
    assert res.payload["comparison"] is None
    assert "哪几款" in (res.narrative_override or "")
    assert sess.get("pending_compare") is not None
