from __future__ import annotations

import sqlite3

import pytest

from store.user_memory_store import PreferenceUpdate, UserMemoryStore, UserPreference


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.sqlite3"
    sqlite3.connect(db).close()
    return UserMemoryStore(db_path=db)


def test_preference_round_trip(store):
    preference = UserPreference(
        user_id="u1",
        personalization_enabled=False,
        budget_min=100,
        budget_max=500,
        price_tier="balanced",
        favorite_categories=["数码电子"],
        brand_include=["Apple"],
        brand_exclude=["Nike"],
        preference_keywords=["轻量", "通勤"],
        style_tags=["轻量"],
        category_specific={"美妆护肤": {"skin_type": "油皮"}},
        preference_note="补充偏好",
        notes="旧备注",
    )

    store.put_preference(preference)
    loaded = store.get_preference("u1")

    assert loaded.to_dict() == preference.to_dict()


def test_missing_preference_returns_empty_profile(store):
    loaded = store.get_preference("new-user")

    assert loaded.user_id == "new-user"
    assert loaded.favorite_categories == []
    assert loaded.brand_include == []


def test_apply_update_returns_undo_token_and_updates_preference(store):
    event = store.apply_update(
        "u1",
        PreferenceUpdate(
            field="brand_exclude",
            value="Apple",
            message="已记住：避免推荐 Apple",
            session_id="s1",
        ),
    )

    assert event is not None
    assert event["undo_token"].startswith("undo_")
    assert store.get_preference("u1").brand_exclude == ["Apple"]


def test_apply_duplicate_array_update_is_noop(store):
    update = PreferenceUpdate(
        field="style_tags",
        value="轻量",
        message="已记住：偏好轻量",
        session_id="s1",
    )

    assert store.apply_update("u1", update) is not None
    assert store.apply_update("u1", update) is None
    assert store.get_preference("u1").style_tags == ["轻量"]


def test_apply_keyword_update(store):
    event = store.apply_update(
        "u1",
        PreferenceUpdate(
            field="preference_keywords",
            value="通勤",
            message="已记住：偏好通勤",
        ),
    )

    assert event is not None
    assert store.get_preference("u1").preference_keywords == ["通勤"]


def test_undo_restores_previous_preference(store):
    first = store.apply_update(
        "u1",
        PreferenceUpdate(
            field="brand_include",
            value="Nike",
            message="已记住：优先考虑 Nike",
        ),
    )
    assert first is not None
    assert store.get_preference("u1").brand_include == ["Nike"]

    undo = store.undo_update("u1", first["undo_token"])

    assert undo is not None
    assert store.get_preference("u1").brand_include == []
    assert store.undo_update("u1", first["undo_token"]) is None
