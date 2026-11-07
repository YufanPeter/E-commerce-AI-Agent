from __future__ import annotations

from unittest.mock import patch

from llm.vision import parsed_query_from_phrase


_TAX = {"sub_to_cat": {"精华": "美妆护肤", "跑步鞋": "服饰运动", "真无线耳机": "数码电子", "耳机": "数码电子"}, "brands": [], "brand_expand": {}}


def test_longest_sub_category_wins_and_category_is_derived():
    with patch("search.query_understanding.load_taxonomy", return_value=_TAX):
        parsed = parsed_query_from_phrase("白色真无线耳机 降噪")
    assert parsed.sub_category == "真无线耳机"
    assert parsed.category == "数码电子"
    assert parsed.retrieval_query == "白色真无线耳机 降噪"
    assert parsed.needs_clarification is False


def test_no_match_leaves_filters_empty():
    with patch("search.query_understanding.load_taxonomy", return_value=_TAX):
        parsed = parsed_query_from_phrase("透明玻璃滴管瓶精华液")
    assert parsed.sub_category == "精华"
    with patch("search.query_understanding.load_taxonomy", return_value=_TAX):
        parsed = parsed_query_from_phrase("一只猫")
    assert parsed.sub_category is None and parsed.category is None
