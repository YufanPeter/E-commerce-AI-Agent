from __future__ import annotations

from search.query_understanding import ParsedQuery
from search.search_service import _apply_user_profile


def test_profile_brand_exclude_applies_when_turn_has_no_brand():
    parsed = ParsedQuery(original_query="推荐手机", category="数码电子")

    merged = _apply_user_profile(parsed, {"brand_exclude": ["Apple"]})

    assert merged.brand_exclude == ["Apple"]


def test_turn_brand_include_overrides_profile_exclude():
    parsed = ParsedQuery(
        original_query="这次看看 Apple",
        category="数码电子",
        brand_include=["Apple 苹果"],
    )

    merged = _apply_user_profile(parsed, {"brand_exclude": ["Apple"]})

    assert merged.brand_include == ["Apple 苹果"]
    assert merged.brand_exclude == []


def test_profile_style_terms_extend_retrieval_query():
    parsed = ParsedQuery(original_query="推荐双肩包", retrieval_query="双肩包")

    merged = _apply_user_profile(parsed, {"preference_keywords": ["轻量", "通勤"]})

    assert merged.soft_terms == ["轻量", "通勤"]
    assert "轻量" in merged.retrieval_query


def test_personalization_disabled_ignores_profile():
    parsed = ParsedQuery(original_query="推荐手机", retrieval_query="手机")

    merged = _apply_user_profile(
        parsed,
        {
            "personalization_enabled": False,
            "brand_exclude": ["Apple"],
            "preference_keywords": ["轻量"],
            "budget_max": 500,
        },
    )

    assert merged == parsed
