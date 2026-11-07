from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import memory_service
from agent.memory_extractor import extract_preference_updates, extract_sensitive_terms
from agent.memory_inference import infer_suggestions
from agent.memory_llm import validate_memories
from store.user_memory_store import MemoryKey, PreferenceUpdate, UserMemoryStore


@pytest.fixture
def store(tmp_path):
    return UserMemoryStore(db_path=tmp_path / "memory.sqlite3")


def _labels(view):
    return [item["label"] for group in view["groups"] for item in group["items"]]


@pytest.mark.parametrize("text", ["推荐通勤的双肩包", "预算500的耳机", "给我妈买个面霜，她是干皮", "有没有42码的"])
def test_one_off_and_third_party_statements_are_not_remembered(text):
    assert extract_preference_updates(text, "u1", "s1") == []


def test_budget_is_scoped_to_the_category_mentioned():
    updates = extract_preference_updates("买手机一般预算3000以内", "u1", "s1")
    assert updates[0].field == "category_budget"
    assert updates[0].value["category"] == "智能手机"


def test_attribute_survives_question_form_and_keeps_evidence():
    updates = extract_preference_updates("我是油皮，用什么洗面奶好", "u1", "s1")
    assert updates[0].value == {"category": "美妆护肤", "key": "skin_type", "value": "油皮"}
    assert updates[0].evidence == "我是油皮，用什么洗面奶好"


def test_youxian_is_not_treated_as_temporary():
    assert extract_preference_updates("以后优先推荐轻量的", "u1", "s1")


def test_other_brands_does_not_look_like_third_party():
    assert extract_preference_updates("以后别再推荐华为，看其他品牌", "u1", "s1")[0].value == "华为"


def test_allergy_is_only_a_suggestion_until_confirmed(store):
    assert extract_sensitive_terms("我对酒精过敏") == ["酒精"]
    memory_service.process_utterance(store, "u1", "我对酒精过敏", "s1")

    assert store.get_preference("u1").avoid_terms == []
    view = memory_service.build_view(store, "u1")
    suggestion = view["suggestions"][0]
    assert suggestion["label"] == "避开 酒精"
    assert suggestion["evidence"] == "我对酒精过敏"

    memory_service.confirm_suggestion(store, "u1", suggestion["id"])
    assert store.get_preference("u1").avoid_terms == ["酒精"]
    assert memory_service.build_view(store, "u1")["suggestions"] == []


def test_ledger_records_source_and_quote(store):
    memory_service.process_utterance(store, "u1", "我一直不买华为", "s1")
    item = memory_service.build_view(store, "u1")["groups"][0]["items"][0]
    assert item["label"] == "不买 华为"
    assert item["evidence"] == "我一直不买华为"
    assert item["source"] == "stated"
    assert "不会再推荐" in item["effect"]


def test_later_statement_overrides_opposite_brand(store):
    memory_service.process_utterance(store, "u1", "我一直不买华为", "s1")
    memory_service.process_utterance(store, "u1", "我喜欢华为", "s1")
    preference = store.get_preference("u1")
    assert preference.brand_include == ["华为"]
    assert preference.brand_exclude == []
    assert _labels(memory_service.build_view(store, "u1")) == ["喜欢 华为"]


def test_delete_updates_preference_and_undo_restores_with_metadata(store):
    memory_service.process_utterance(store, "u1", "我一直不买华为", "s1")
    item = memory_service.build_view(store, "u1")["groups"][0]["items"][0]

    result = store.remove_item("u1", item["id"])
    assert store.get_preference("u1").brand_exclude == []
    assert memory_service.build_view(store, "u1")["count"] == 0

    store.undo_update("u1", result["undo_token"])
    restored = memory_service.build_view(store, "u1")["groups"][0]["items"][0]
    assert store.get_preference("u1").brand_exclude == ["华为"]
    assert restored["id"] == item["id"]
    assert restored["evidence"] == "我一直不买华为"


def test_put_preference_appears_in_ledger_and_removal_is_tracked(store):
    from store.user_memory_store import UserPreference

    store.put_preference(UserPreference(user_id="u1", brand_exclude=["Apple"], budget_max=500))
    assert sorted(_labels(memory_service.build_view(store, "u1"))) == ["不买 Apple", "常用预算 ¥500 以内"]

    store.put_preference(UserPreference(user_id="u1"))
    assert _labels(memory_service.build_view(store, "u1")) == []


def test_legacy_category_name_is_normalized(store):
    from store.user_memory_store import UserPreference

    store.put_preference(UserPreference(user_id="u1", favorite_categories=["食品生活"]))
    assert store.get_preference("u1").favorite_categories == ["食品饮料"]


def test_tell_falls_back_to_note_when_nothing_structured(store, monkeypatch):
    result = memory_service.tell(store, "u1", "喜欢简约一点的设计")
    assert result["message"]
    assert store.get_preference("u1").preference_note == "喜欢简约一点的设计"


def test_clear_user_forgets_everything(store):
    memory_service.process_utterance(store, "u1", "我是油皮", "s1")
    store.record_events("u1", "s1", [{"type": "search", "payload": {}}])
    store.clear_user("u1")
    view = memory_service.build_view(store, "u1")
    assert view["count"] == 0
    assert view["activity"] == []


# --------------------------- 行为事件与推断 ---------------------------

def _event(kind, brand="小米", days_ago=0, **payload):
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"type": kind, "brand": brand, "created_at": created.strftime("%Y-%m-%d %H:%M:%S"), "payload": payload}


def test_repeated_cart_adds_suggest_brand_with_evidence():
    suggestions = infer_suggestions([_event("add_cart"), _event("add_cart")])
    assert suggestions[0].key == MemoryKey("brand_like", value="小米")
    assert "加购 2 次" in suggestions[0].evidence


def test_old_behaviour_decays_below_threshold():
    assert infer_suggestions([_event("add_cart", days_ago=120), _event("add_cart", days_ago=120)]) == []


def test_consistent_budget_is_inferred_but_scattered_is_not():
    steady = [_event("search", sub_category="智能手机", max_price=p) for p in (3000, 3000, 3500)]
    assert infer_suggestions(steady)[0].key == MemoryKey("budget", scope="智能手机", value="-3000")
    scattered = [_event("search", sub_category="智能手机", max_price=p) for p in (1000, 3000, 9000)]
    assert infer_suggestions(scattered) == []


def test_inference_only_suggests_and_rejection_sticks(store):
    store.record_events("u1", "s1", [
        {"type": "add_cart", "brand": "小米", "product_id": "p1"},
        {"type": "add_cart", "brand": "小米", "product_id": "p2"},
    ])
    view = memory_service.build_view(store, "u1")
    assert store.get_preference("u1").brand_include == []
    suggestion = view["suggestions"][0]
    assert suggestion["source"] == "inferred"

    memory_service.reject_suggestion(store, "u1", suggestion["id"])
    assert memory_service.build_view(store, "u1")["suggestions"] == []


def test_inference_does_not_repeat_a_brand_under_another_spelling(store):
    memory_service.process_utterance(store, "u1", "我喜欢耐克", "s1")
    assert store.get_preference("u1").brand_include == ["Nike"]
    store.record_events("u1", "s1", [
        {"type": "add_cart", "brand": "耐克", "product_id": "p1"},
        {"type": "add_cart", "brand": "Nike", "product_id": "p2"},
    ])
    assert memory_service.build_view(store, "u1")["suggestions"] == []


def test_events_from_tool_results():
    search = memory_service.events_from_tool_result(
        {"tool_name": "recommend", "payload": {"products": [{}], "debug": {"parsed": {"sub_category": "智能手机", "max_price": 3000}}}},
        "3000以内的手机",
    )
    assert search[0]["type"] == "search"
    assert search[0]["payload"]["max_price"] == 3000
    assert memory_service.events_from_tool_result({"tool_name": "recommend", "payload": {"products": []}}, "x") == []
    add = memory_service.events_from_tool_result(
        {"tool_name": "cart", "payload": {"action": "add", "added": {"product_id": "p1", "title": "T"}}}, "加购"
    )
    assert add[0]["type"] == "add_cart"


def test_used_counter_increments_for_applied_signals(store):
    store.apply_update("u1", PreferenceUpdate(field="brand_exclude", value="华为", message="m"))
    memory_service.record_turn(
        store, "u1", "s1",
        {"tool_name": "recommend", "payload": {"products": [], "personalization": [{"kind": "brand_avoid", "value": "华为", "scope": ""}]}},
        "推荐手机",
    )
    assert memory_service.build_view(store, "u1")["groups"][0]["items"][0]["use_count"] == 1


# --------------------------- 模型输出校验 ---------------------------

def test_purchase_intent_does_not_reach_the_model():
    from agent.memory_llm import might_disclose

    assert not might_disclose("我想看看华为手机")
    assert not might_disclose("帮我推荐手机")
    assert might_disclose("家里两个娃，零食要小包装的")


def test_llm_memory_without_real_quote_is_dropped():
    raw = [{"kind": "style", "value": "轻量", "quote": "我喜欢轻的", "durable": True}]
    assert validate_memories(raw, "推荐手机") == []


def test_llm_one_off_memory_is_dropped():
    raw = [{"kind": "style", "value": "轻量", "quote": "轻便", "durable": False}]
    assert validate_memories(raw, "推荐轻便的电脑") == []


def test_llm_blocking_memory_requires_confirmation():
    raw = [{"kind": "avoid_term", "value": "酒精", "quote": "不用含酒精", "durable": True}]
    assert validate_memories(raw, "我从来不用含酒精的护肤品")[0].needs_confirm is True


def _search_result():
    return {
        "tool_name": "recommend",
        "payload": {"products": [{"product_id": "p1"}], "debug": {"parsed": {"category": "美妆护肤"}}},
    }


@pytest.mark.parametrize("query", ["更多防晒打底，轻薄不闷", "换一批", "还有别的吗"])
def test_see_more_is_not_recorded_as_a_new_search(query):
    assert memory_service.events_from_tool_result(_search_result(), query) == []


def test_real_search_is_still_recorded():
    events = memory_service.events_from_tool_result(_search_result(), "推荐防晒")
    assert [e["type"] for e in events] == ["search"]


def test_activity_hides_see_more_recorded_earlier(store):
    for query in ("推荐防晒", "更多防晒打底，轻薄不闷", "推荐耳机"):
        event = {"type": "search", "payload": {"query": query}}
        store.record_events("u1", "s1", [event])
    texts = [a["text"] for a in memory_service.build_view(store, "u1")["activity"]]
    assert texts == ["搜索 推荐耳机", "搜索 推荐防晒"]
