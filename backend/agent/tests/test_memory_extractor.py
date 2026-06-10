from __future__ import annotations

from agent.memory_extractor import extract_preference_updates


def test_extract_brand_exclude():
    updates = extract_preference_updates("以后不要给我推荐苹果", "u1", "s1")

    assert len(updates) == 1
    assert updates[0].field == "brand_exclude"
    assert updates[0].value == "Apple"


def test_extract_style_preference():
    updates = extract_preference_updates("以后推荐轻一点的通勤款", "u1", "s1")

    assert [(u.field, u.value) for u in updates] == [
        ("preference_keywords", "轻量"),
        ("preference_keywords", "通勤"),
    ]


def test_extract_budget_range():
    updates = extract_preference_updates("我一般预算 300 到 500", "u1", "s1")

    assert updates[0].field == "budget_range"
    assert updates[0].value == {"budget_min": 300.0, "budget_max": 500.0}


def test_temporary_or_question_does_not_write():
    assert extract_preference_updates("这次想看看苹果", "u1", "s1") == []
    assert extract_preference_updates("苹果手机怎么样", "u1", "s1") == []


def test_high_risk_does_not_write():
    assert extract_preference_updates("我对酒精过敏", "u1", "s1") == []
