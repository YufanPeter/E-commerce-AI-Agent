from __future__ import annotations

"""记忆层编排：把抽取、推断、事件和存储串起来，给 API 用。

一条记忆的三种来路，用法不同：

    你说的（stated / manual）   立即生效，可撤销。品牌排除和忌讳是硬过滤，其余只影响排序。
    模型听出来的（LLM）         能挡掉商品的要确认，其余生效并给依据原话。
    行为推断（inferred）        永远先进待确认，确认前只有品牌给很小的排序加权。
"""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent.memory_extractor import _STYLE_ALIASES, extract_preference_updates, extract_sensitive_terms
from agent.memory_inference import infer_suggestions
from agent.memory_llm import ExtractedMemory, extract_with_llm, might_disclose
from agent.tools.refine import parse_more_request
from catalog_config import AMAZON_MODE
from search.query_understanding import expand_brands
from store.user_memory_store import (
    ACTIVE,
    ATTR_LABELS,
    REJECTED,
    SUGGESTED,
    MemoryKey,
    PreferenceUpdate,
    UserMemoryStore,
)


logger = logging.getLogger(__name__)

_llm_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-llm")
_MIN_INFERRED_CONFIDENCE = 0.6

_GROUPS = {
    "budget": "预算",
    "price_tier": "预算",
    "brand_like": "品牌",
    "brand_avoid": "品牌",
    "attribute": "关于你",
    "avoid_term": "关于你",
    "style": "风格",
    "category": "常看",
    "note": "其他",
}
_GROUP_ORDER = ["关于你", "预算", "品牌", "风格", "常看", "其他"]
_TIER_LABELS = {"value": "偏好高性价比", "premium": "偏好品质款"}
_SOURCE_LABELS = {
    "stated": "你在对话里说的",
    "manual": "你亲手加的",
    "confirmed": "你确认过的",
    "inferred": "从你的行为推测",
}
_EFFECTS = {
    "brand_avoid": "硬性排除，不会再推荐",
    "avoid_term": "硬性排除，含这个成分的不推荐",
    "brand_like": "排序时优先",
    "budget": "排序时优先，超出的也还能看到",
    "price_tier": "排序时参考",
    "style": "作为搜索偏好",
    "category": "没说品类时优先",
    "attribute": "解说时结合",
    "note": "解说时结合",
}

if AMAZON_MODE:
    _GROUPS = {
        "budget": "Budget", "price_tier": "Budget", "brand_like": "Brands", "brand_avoid": "Brands",
        "attribute": "About you", "avoid_term": "About you", "style": "Style",
        "category": "Categories", "note": "Notes",
    }
    _GROUP_ORDER = ["About you", "Budget", "Brands", "Style", "Categories", "Notes"]
    _TIER_LABELS = {"value": "Prefer value", "premium": "Prefer premium products"}
    _SOURCE_LABELS = {"stated": "Stated in chat", "manual": "Added by you", "confirmed": "Confirmed by you", "inferred": "Inferred from activity"}
    _EFFECTS = {
        "brand_avoid": "Excluded from recommendations", "brand_like": "Preferred in ranking",
        "budget": "Used as the default budget", "note": "Saved for reference",
    }


def llm_extract_enabled() -> bool:
    return os.getenv("MEMORY_LLM_EXTRACT", "1").strip().lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------

def _money_range(value: str) -> str:
    low, _, high = value.partition("-")
    if AMAZON_MODE:
        return f"${low} – ${high}" if low and high else (f"Up to ${high}" if high else f"At least ${low}")
    if low and high:
        return f"¥{low} – ¥{high}"
    if high:
        return f"¥{high} 以内"
    return f"¥{low} 以上"


def item_label(item: dict[str, Any]) -> str:
    kind, value, scope = item["kind"], item["value"], item.get("scope") or ""
    if AMAZON_MODE:
        if kind == "budget":
            return f"{scope or 'Default'} budget: {_money_range(value)}"
        if kind in {"brand_like", "brand_avoid", "avoid_term", "category"}:
            prefix = {"brand_like": "Likes", "brand_avoid": "Avoids", "avoid_term": "Avoid", "category": "Often browses"}[kind]
            return f"{prefix} {value}"
    if kind == "budget":
        return f"{scope or '常用'}预算 {_money_range(value)}"
    if kind == "price_tier":
        return _TIER_LABELS.get(value, value)
    if kind == "brand_like":
        return f"喜欢 {value}"
    if kind == "brand_avoid":
        return f"不买 {value}"
    if kind == "avoid_term":
        return f"避开 {value}"
    if kind == "attribute":
        return f"{ATTR_LABELS.get(item.get('attr'), item.get('attr'))}：{value}"
    if kind == "category":
        return f"常看 {value}"
    return value


def item_view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["item_id"],
        "kind": item["kind"],
        "group": _GROUPS.get(item["kind"], _GROUP_ORDER[-1]),
        "label": item_label(item),
        "value": item["value"],
        "scope": item.get("scope") or None,
        "status": item["status"],
        "source": item["source"],
        "source_label": _SOURCE_LABELS.get(item["source"], item["source"]),
        "evidence": item.get("evidence"),
        "effect": _EFFECTS.get(item["kind"], ""),
        "needs_care": item["kind"] == "avoid_term",
        "use_count": item.get("use_count") or 0,
        "last_used_at": item.get("last_used_at"),
        "created_at": item.get("created_at"),
    }


def _summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    ordered = sorted(items, key=lambda i: _GROUP_ORDER.index(_GROUPS.get(i["kind"], _GROUP_ORDER[-1])))
    labels = [item_label(item) for item in ordered if item["kind"] != "note"][:4]
    return (", " if AMAZON_MODE else "、").join(labels)


_ACTIVITY_LABELS = {"search": "搜索", "view_detail": "看了", "compare": "对比了", "add_cart": "加购了", "remove_cart": "移出了", "checkout": "下单了"}
if AMAZON_MODE:
    _ACTIVITY_LABELS = {"search": "Searched", "view_detail": "Viewed", "compare": "Compared", "add_cart": "Added", "remove_cart": "Removed", "checkout": "Ordered"}


def _activity(store: UserMemoryStore, user_id: str, limit: int = 8) -> list[dict[str, Any]]:
    out = []
    # 多取一些：下面会滤掉早期版本记下的"看更多"
    for event in store.recent_events(user_id, days=30, limit=limit * 3):
        subject = event.get("title") or (event.get("payload") or {}).get("query") or ""
        if event["type"] == "search" and parse_more_request(subject) is not None:
            continue
        if len(out) == limit:
            break
        out.append({
            "id": str(event["id"]),
            "type": event["type"],
            "text": f"{_ACTIVITY_LABELS.get(event['type'], event['type'])} {subject}".strip(),
            "at": event["created_at"],
        })
    return out


_INTEREST_EVENT_TYPES = {"search", "view_detail", "compare", "add_cart"}


def recent_interests(store: UserMemoryStore, user_id: str, limit: int = 2) -> dict[str, Any]:
    """首页"接着看"用：用户最近在看的品类，新的在前，不重复。

    用户关掉个性化时不返回品类，首页就只显示通用入口。"""
    events = store.recent_events(user_id, days=30, limit=60)
    interests: list[dict[str, Any]] = []
    if store.get_preference(user_id).personalization_enabled:
        seen: set[str] = set()
        for event in events:
            if event["type"] not in _INTEREST_EVENT_TYPES:
                continue
            label = event.get("sub_category") or event.get("category")
            if not label or label in seen:
                continue
            seen.add(label)
            interests.append({
                "label": label,
                "sub_category": event.get("sub_category"),
                "category": event.get("category"),
                "product_id": event.get("product_id"),
            })
            if len(interests) == limit:
                break
    return {"returning": bool(events), "interests": interests}


def build_view(store: UserMemoryStore, user_id: str) -> dict[str, Any]:
    if not AMAZON_MODE:
        refresh_inferred(store, user_id)
    preference = store.get_preference(user_id)
    rows = store.list_items(user_id)
    active = [row for row in rows if row["status"] == ACTIVE]
    suggested = [row for row in rows if row["status"] == SUGGESTED]
    groups = []
    for name in _GROUP_ORDER:
        members = [item_view(row) for row in active if _GROUPS.get(row["kind"], _GROUP_ORDER[-1]) == name]
        if members:
            groups.append({"name": name, "items": members})
    return {
        "user_id": user_id,
        "personalization_enabled": preference.personalization_enabled,
        "summary": _summary(active),
        "groups": groups,
        "suggestions": [item_view(row) for row in suggested],
        "activity": _activity(store, user_id),
        "count": len(active),
    }


# ---------------------------------------------------------------------------
# 对话里的记忆
# ---------------------------------------------------------------------------

def _update_from_extracted(memory: ExtractedMemory, text: str, session_id: str | None) -> PreferenceUpdate | None:
    common = {"session_id": session_id, "evidence": memory.quote or text, "confidence": 0.8}
    value = memory.value
    if memory.kind == "brand_like":
        return PreferenceUpdate(field="brand_include", value=value, message=f"已记住：优先考虑 {value}", **common)
    if memory.kind == "style":
        value = _STYLE_ALIASES.get(value, value)
        return PreferenceUpdate(field="preference_keywords", value=value, message=f"已记住：偏好{value}", **common)
    if memory.kind == "budget":
        return PreferenceUpdate(field="budget_max", value=float(value), message=f"已记住：常用预算 ¥{value} 以内", **common)
    attr = {"skin_type": ("美妆护肤", "skin_type"), "shoe_size": ("服饰运动", "shoe_size"), "clothing_size": ("服饰运动", "clothing_size"), "household": ("通用", "household")}.get(memory.kind)
    if attr:
        return PreferenceUpdate(
            field="category_attr",
            value={"category": attr[0], "key": attr[1], "value": value},
            message=f"已记住：{ATTR_LABELS[attr[1]]} {value}",
            **common,
        )
    return None


def _key_from_extracted(memory: ExtractedMemory) -> MemoryKey | None:
    if memory.kind in {"brand_avoid", "avoid_term"}:
        return MemoryKey(memory.kind, value=memory.value)
    return None


def _apply_extracted(store: UserMemoryStore, user_id: str, memories: list[ExtractedMemory], text: str, session_id: str | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for memory in memories:
        if memory.needs_confirm:
            key = _key_from_extracted(memory)
            if key:
                store.suggest_item(user_id, key, source="stated", evidence=memory.quote, confidence=0.8, session_id=session_id)
            continue
        update = _update_from_extracted(memory, text, session_id)
        if update:
            event = store.apply_update(user_id, update)
            if event:
                events.append(event)
    return events


def process_utterance(store: UserMemoryStore, user_id: str, text: str, session_id: str | None) -> list[dict[str, Any]]:
    """规则抽取，同步执行，结果立即生效。"""
    if AMAZON_MODE:
        if not re.match(r"\s*(?:remember\b|i usually\b|my (?:usual )?budget is usually\b)", text, re.I):
            return []
        return _english_memory_update(store, user_id, text, session_id, source="stated")
    events: list[dict[str, Any]] = []
    for update in extract_preference_updates(text, user_id, session_id):
        event = store.apply_update(user_id, update)
        if event is not None:
            events.append(event)
    for term in extract_sensitive_terms(text):
        store.suggest_item(user_id, MemoryKey("avoid_term", value=term), source="stated", evidence=text, confidence=0.9, session_id=session_id)
    return events


def extract_in_background(store: UserMemoryStore, user_id: str, text: str, session_id: str | None, already: list[dict[str, Any]]) -> None:
    """规则没抽到东西时，后台再让模型听一遍。不阻塞对话，结果下次打开页面可见。"""
    if already or not llm_extract_enabled() or not might_disclose(text):
        return

    def _run() -> None:
        try:
            _apply_extracted(store, user_id, extract_with_llm(text), text, session_id)
        except Exception:  # noqa: BLE001
            logger.exception("后台记忆抽取失败")

    _llm_pool.submit(_run)


def _english_memory_update(
    store: UserMemoryStore, user_id: str, text: str, session_id: str | None, source: str,
) -> list[dict[str, Any]]:
    clean = re.sub(r"^\s*remember(?: that)?\s+", "", text.strip(), flags=re.I).rstrip(".!?")
    brand = re.fullmatch(r"i (?:usually |always )?(like|prefer|avoid|never buy) (.{1,55})", clean, re.I)
    budget = re.fullmatch(
        r"my (?:usual )?budget is (?:usually )?(?:between )?\$?(\d+(?:\.\d{1,2})?)(?:\s+(?:and|to)\s+\$?(\d+(?:\.\d{1,2})?))?",
        clean, re.I,
    )
    if brand:
        field = "brand_include" if brand[1].lower() in {"like", "prefer"} else "brand_exclude"
        value: Any = brand[2].strip()
    elif budget:
        field = "budget_range"
        value = {"budget_min": float(budget[1]) if budget[2] else None, "budget_max": float(budget[2] or budget[1])}
        if value["budget_min"] is not None and value["budget_min"] > value["budget_max"]:
            return []
    elif source == "manual" and clean:
        field = "preference_note"
        existing = store.get_preference(user_id).preference_note
        value = f"{existing}; {clean}" if existing else clean
    else:
        return []
    event = store.apply_update(user_id, PreferenceUpdate(
        field=field, value=value, message=f"Saved: {clean}", source=source, session_id=session_id, evidence=text,
    ))
    return [event] if event else []


def tell(store: UserMemoryStore, user_id: str, text: str) -> dict[str, Any]:
    """用户在记忆页直接说一句话。规则 → 模型 → 都没听出结构就原样记成备注。"""
    clean = (text or "").strip()
    if AMAZON_MODE:
        events = _english_memory_update(store, user_id, clean, None, source="manual")
        return {"updates": events, "message": "Memory saved." if events else "No change to saved memory."}
    if not clean:
        return {"updates": [], "message": "没听清，再说一次？"}
    before = {row["item_id"] for row in store.list_items(user_id)}
    events = process_utterance(store, user_id, clean, None)
    if not events and llm_extract_enabled():
        events = _apply_extracted(store, user_id, extract_with_llm(clean), clean, None)
    after = store.list_items(user_id)
    new_suggestions = [row for row in after if row["item_id"] not in before and row["status"] == SUGGESTED]
    if events:
        message = "；".join(event["message"].removeprefix("已记住：") for event in events)
        return {"updates": events, "message": f"记住了：{message}"}
    if new_suggestions:
        return {"updates": [], "message": "这条会影响能看到的商品，请在上面确认一下"}
    existing = store.get_preference(user_id).preference_note
    merged = f"{existing}；{clean}" if existing else clean
    event = store.apply_update(user_id, PreferenceUpdate(field="preference_note", value=merged, message=f"已记住：{clean}", source="manual", evidence=clean))
    return {"updates": [event] if event else [], "message": "记下了，解说时会参考"}


def confirm_suggestion(store: UserMemoryStore, user_id: str, item_id: str) -> dict[str, Any] | None:
    item = store.get_item(user_id, item_id)
    if item is None:
        return None
    update = _update_from_item(item)
    if update is None:
        return None
    event = store.apply_update(user_id, update)
    store.mark_confirmed(user_id, item_id)
    return event or {"message": f"已记住：{item_label(item)}"}


def _update_from_item(item: dict[str, Any]) -> PreferenceUpdate | None:
    kind, value, scope = item["kind"], item["value"], item.get("scope") or ""
    common = {"message": f"Saved: {item_label(item)}" if AMAZON_MODE else f"已记住：{item_label(item)}",
              "source": "confirmed", "evidence": item.get("evidence")}
    if kind == "brand_like":
        return PreferenceUpdate(field="brand_include", value=value, **common)
    if kind == "brand_avoid":
        return PreferenceUpdate(field="brand_exclude", value=value, **common)
    if kind == "avoid_term":
        return PreferenceUpdate(field="avoid_terms", value=value, **common)
    if kind == "category":
        return PreferenceUpdate(field="favorite_categories", value=value, **common)
    if kind == "style":
        return PreferenceUpdate(field="preference_keywords", value=value, **common)
    if kind == "budget":
        low, _, high = value.partition("-")
        bounds = {"budget_min": float(low) if low else None, "budget_max": float(high) if high else None}
        if scope:
            return PreferenceUpdate(field="category_budget", value={"category": scope, **bounds}, **common)
        return PreferenceUpdate(field="budget_range", value=bounds, **common)
    return None


def reject_suggestion(store: UserMemoryStore, user_id: str, item_id: str) -> bool:
    if store.get_item(user_id, item_id) is None:
        return False
    store.set_item_status(user_id, item_id, REJECTED)
    return True


# ---------------------------------------------------------------------------
# 行为事件与推断
# ---------------------------------------------------------------------------

def events_from_tool_result(tool_result: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """把一次工具结果翻译成行为事件。只记真实发生的动作，追问和报错不算。"""
    name = tool_result.get("tool_name")
    payload = tool_result.get("payload") or {}
    if name == "recommend":
        parsed = (payload.get("debug") or {}).get("parsed") or {}
        if not payload.get("products"):
            return []
        # "看更多"是同一次搜索的延续，算进去会重复计数，也会把分区小标题显示成用户的搜索词
        if parse_more_request(query) is not None:
            return []
        return [{
            "type": "search",
            "category": parsed.get("category"),
            "sub_category": parsed.get("sub_category"),
            "payload": {
                "query": query,
                "category": parsed.get("category"),
                "sub_category": parsed.get("sub_category"),
                "max_price": parsed.get("max_price"),
                "min_price": parsed.get("min_price"),
            },
        }]
    if name == "product_detail":
        product = payload.get("product") or {}
        if not product.get("product_id"):
            return []
        return [_product_event("view_detail", product)]
    if name == "compare":
        return [_product_event("compare", item) for item in (payload.get("comparison") or {}).get("products") or []]
    if name == "cart":
        action = payload.get("action")
        if action == "add" and payload.get("added"):
            return [_product_event("add_cart", payload["added"])]
        if action == "reorder":
            return [_product_event("add_cart", line) for line in payload.get("added_lines") or []]
        if action == "remove" and payload.get("removed"):
            return [_product_event("remove_cart", payload["removed"])]
        if action == "checkout":
            lines = (payload.get("order") or {}).get("lines") or []
            return [_product_event("checkout", line) for line in lines]
    return []


def _product_event(kind: str, product: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": kind,
        "product_id": product.get("product_id"),
        "title": product.get("title"),
        "brand": product.get("brand"),
        "category": product.get("category"),
        "sub_category": product.get("sub_category"),
        "price": product.get("price") if isinstance(product.get("price"), (int, float)) else None,
    }


def _fill_product_facts(events: list[dict[str, Any]]) -> None:
    """购物车行里没有品牌和类目，从商品库补齐，推断才用得上。"""
    missing = [ev for ev in events if ev.get("product_id") and not ev.get("brand")]
    if not missing:
        return
    try:
        from store.product_store import ProductStore

        products = ProductStore()
        for event in missing:
            detail = products.get_product_detail(event["product_id"])
            if detail is not None:
                event.update(brand=detail.brand, category=detail.category, sub_category=detail.sub_category)
                event["title"] = event.get("title") or detail.title
    except Exception:  # noqa: BLE001 - 补不齐也不影响记录
        logger.warning("补全事件商品信息失败", exc_info=True)


def record_turn(store: UserMemoryStore, user_id: str | None, session_id: str | None, tool_result: dict[str, Any], query: str) -> None:
    if not user_id:
        return
    try:
        events = events_from_tool_result(tool_result, query)
        _fill_product_facts(events)
        store.record_events(user_id, session_id, events)
        _mark_used(store, user_id, (tool_result.get("payload") or {}).get("personalization") or [])
        if events:
            refresh_inferred(store, user_id)
    except Exception:  # noqa: BLE001 - 记录失败不能影响对话
        logger.exception("记录行为事件失败")


def _mark_used(store: UserMemoryStore, user_id: str, signals: list[dict[str, Any]]) -> None:
    if not signals:
        return
    rows = store.list_items(user_id, statuses=(ACTIVE,))
    keys: list[MemoryKey] = []
    for signal in signals:
        kind = signal.get("kind")
        for row in rows:
            if row["kind"] != kind:
                continue
            matched = False
            if kind in {"budget", "price_tier"}:
                matched = (row.get("scope") or "") == (signal.get("scope") or "")
            elif kind in {"brand_like", "brand_avoid"}:
                matched = signal.get("value") in expand_brands([row["value"]]) or signal.get("value") == row["value"]
            else:
                matched = signal.get("value") == row["value"]
            if matched:
                keys.append(MemoryKey(row["kind"], row.get("scope") or "", row.get("attr") or "", row["value"]))
    store.mark_used(user_id, list(dict.fromkeys(keys)))


def refresh_inferred(store: UserMemoryStore, user_id: str) -> None:
    """按最新行为重算建议：新出现的登记，信号消失的撤回，用户处理过的不再提。"""
    if AMAZON_MODE:
        return
    suggestions = infer_suggestions(store.recent_events(user_id))
    current = {
        MemoryKey(row["kind"], row.get("scope") or "", row.get("attr") or "", row["value"])
        for row in store.list_items(user_id, statuses=(SUGGESTED,))
        if row["source"] == "inferred"
    }
    # 同一品牌有多种写法（Nike / 耐克）：用户已经表过态的不再猜，换个写法也一样。
    settled = store.list_items(user_id, statuses=(ACTIVE, REJECTED))
    settled_brands = set(expand_brands([row["value"] for row in settled if row["kind"] in {"brand_like", "brand_avoid"}]))
    suggestions = [
        suggestion for suggestion in suggestions
        if not (suggestion.key.kind == "brand_like" and settled_brands & set(expand_brands([suggestion.key.value])))
    ]
    fresh = {suggestion.key for suggestion in suggestions}
    for key in current - fresh:
        store.retract_suggestion(user_id, key)
    for suggestion in suggestions:
        store.suggest_item(user_id, suggestion.key, source="inferred", evidence=suggestion.evidence, confidence=suggestion.confidence)


def inferred_signals(store: UserMemoryStore, user_id: str) -> dict[str, Any]:
    """未确认的推断里，给检索用的弱信号。过敏等敏感内容不在其中。"""
    try:
        rows = store.list_items(user_id, statuses=(SUGGESTED,))
    except Exception:  # noqa: BLE001
        return {}
    brands = [
        row["value"] for row in rows
        if row["kind"] == "brand_like" and row["source"] == "inferred" and row["confidence"] >= _MIN_INFERRED_CONFIDENCE
    ]
    return {"brand_like": brands} if brands else {}
