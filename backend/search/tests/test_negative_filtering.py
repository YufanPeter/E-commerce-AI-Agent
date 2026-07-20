from __future__ import annotations

"""否定/反选（品类反选结构化 + 归一 + 正则兜底）的单元测试。

覆盖三层防线：
  P0 结构化：where_builder 把 sub_category_exclude / category_exclude 翻成 $nin
  P1 归一：_normalize_negatives 把"能量饮料/功能性饮料"收敛到"功能饮料"
  P2 兜底：LLM 失败降级时 _regex_extract_excludes 从原句抽品类反选
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from search.query_understanding import (
    ParsedQuery,
    _regex_extract_brand_excludes,
    expand_brands,
    _regex_extract_excludes,
    understand_query,
)
from search.where_builder import build_chroma_where
from search.search_service import _normalize_negatives


# --------------------------- P0: structured exclusions in `where` ---------------------------

def test_sub_category_exclude_becomes_nin():
    parsed = ParsedQuery(
        original_query="推荐饮料 不要功能饮料",
        category="食品生活",
        sub_category_exclude=["功能饮料"],
    )
    where = build_chroma_where(parsed)
    assert where == {
        "$and": [
            {"category": "食品生活"},
            {"sub_category": {"$nin": ["功能饮料"]}},
        ]
    }


def test_category_exclude_becomes_nin():
    parsed = ParsedQuery(
        original_query="推荐吃的 不要美妆",
        category_exclude=["美妆护肤"],
    )
    where = build_chroma_where(parsed)
    assert where == {"category": {"$nin": ["美妆护肤"]}}


def test_brand_exclude_alias_expands_variants():
    parsed = ParsedQuery(
        original_query="非华为非苹果的手机",
        brand_exclude=["华为", "苹果"],
    )
    where = build_chroma_where(parsed)
    excluded = where["brand"]["$nin"]
    assert "华为" in excluded
    assert "Apple 苹果" in excluded
    assert "苹果" in excluded


def test_expand_brands_accepts_alias_variant():
    expanded = expand_brands(["苹果", "Apple"])
    assert "Apple 苹果" in expanded
    assert "苹果" in expanded


def test_regex_extracts_compact_brand_excludes():
    excludes = _regex_extract_brand_excludes("非华为非苹果的优质数码好物")
    assert "华为" in excludes
    assert "苹果" in excludes


def test_successful_llm_parse_still_merges_regex_brand_excludes(monkeypatch):
    monkeypatch.setattr(
        "search.query_understanding._cached_understand",
        lambda query: ParsedQuery(original_query=query, retrieval_query=query),
    )

    parsed = understand_query("非华为非苹果的手机")

    assert "华为" in parsed.brand_exclude
    assert "苹果" in parsed.brand_exclude


def test_multiple_sub_excludes():
    parsed = ParsedQuery(
        original_query="x",
        sub_category="茶饮",
        sub_category_exclude=["功能饮料", "碳酸饮料"],
    )
    where = build_chroma_where(parsed)
    assert {"sub_category": "茶饮"} in where["$and"]
    assert {"sub_category": {"$nin": ["功能饮料", "碳酸饮料"]}} in where["$and"]


# --------------------------- P1: negative-term normalization ---------------------------

def test_normalize_collapses_synonyms_to_canonical():
    assert _normalize_negatives(["能量饮料"]) == ["功能饮料"]
    assert _normalize_negatives(["功能性饮料"]) == ["功能饮料"]
    assert _normalize_negatives(["提神饮料"]) == ["功能饮料"]


def test_normalize_dedups():
    # Multiple synonyms should collapse to one normalized value.
    assert _normalize_negatives(["能量饮料", "功能性饮料", "功能饮料"]) == ["功能饮料"]


def test_normalize_passthrough_non_alias():
    # Non-alias ingredient terms should remain unchanged.
    assert _normalize_negatives(["花生", "香菜"]) == ["花生", "香菜"]


def test_normalize_empty_safe():
    assert _normalize_negatives(None) == []
    assert _normalize_negatives([]) == []


# --------------------------- P2: regex fallback after LLM failure ---------------------------

def test_regex_extracts_sub_exclude_with_synonym():
    sub, cat = _regex_extract_excludes("推荐饮料，不要功能性饮料")
    assert "功能饮料" in sub
    assert cat == []


def test_regex_extracts_carbonated():
    sub, cat = _regex_extract_excludes("来点饮料 不要碳酸的")
    assert "碳酸饮料" in sub


def test_regex_zero_false_positive_on_price():
    # A price constraint without a category must not produce a false category exclusion.
    sub, cat = _regex_extract_excludes("推荐饮料，不要太贵")
    assert sub == []
    assert cat == []


def test_regex_no_negation_cue_returns_empty():
    # Return no exclusions when the query has no negative trigger.
    sub, cat = _regex_extract_excludes("推荐功能饮料")
    assert sub == []
    assert cat == []


# ----------------- P2: fallback exclusions across all categories -----------------

def test_regex_beauty_official_and_synonym():
    # Beauty: both official names and colloquial synonyms should match.
    assert "面膜" in _regex_extract_excludes("推荐美妆，不要面膜")[0]
    assert "面膜" in _regex_extract_excludes("推荐护肤，不要补水面膜")[0]
    assert "洁面" in _regex_extract_excludes("推荐护肤，不要洗面奶")[0]
    assert "唇釉" in _regex_extract_excludes("推荐美妆，不要口红")[0]


def test_regex_digital_synonym():
    # Electronics: normalize colloquial product types to their catalog names.
    assert "真无线耳机" in _regex_extract_excludes("推荐数码，不要蓝牙耳机")[0]
    assert "平板电脑" in _regex_extract_excludes("推荐数码，不要平板")[0]


def test_regex_apparel_synonym():
    # Apparel: normalize colloquial running-shoe wording while preserving official names.
    assert "跑步鞋" in _regex_extract_excludes("推荐运动鞋，不要慢跑鞋")[0]
    assert "卫衣" in _regex_extract_excludes("推荐衣服，不要卫衣")[0]


def test_regex_slash_subcategory_matches_each_part():
    # Slash-separated catalog subcategories must match either component in a negative request.
    assert "坚果/零食" in _regex_extract_excludes("推荐零食，不要坚果")[0]
    assert "坚果/零食" in _regex_extract_excludes("来点吃的，不要零食")[0]


def test_regex_no_false_positive_on_plain_request():
    # Never extract a category without both a negative trigger and a category reference.
    assert _regex_extract_excludes("随便推荐点东西") == ([], [])
    assert _regex_extract_excludes("推荐无糖饮料") == ([], [])
