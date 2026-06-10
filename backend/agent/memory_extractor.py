from __future__ import annotations

"""Deterministic V1 preference extractor.

The product plan intentionally avoids a second LLM write path in V1. This
extractor only handles explicit, low-risk preference statements and ignores
temporary, ambiguous, question-like, and health/allergy/diet statements.
"""

import re
from typing import Any

from store.user_memory_store import PreferenceUpdate


_TEMPORARY_MARKERS = ("这次", "今天", "先", "暂时", "试试", "看看", "当前", "本次")
_QUESTION_MARKERS = ("吗", "么", "怎么样", "如何", "好不好", "哪个好", "？", "?")
_HIGH_RISK_MARKERS = ("过敏", "忌口", "不能吃", "不耐受", "孕妇", "哺乳", "疾病", "医生")

_KNOWN_BRANDS = (
    "Apple", "苹果", "Nike", "耐克", "Adidas", "阿迪达斯", "小米", "华为",
    "vivo", "OPPO", "兰蔻", "雅诗兰黛", "The North Face", "北面",
)
_BRAND_CANONICAL = {
    "苹果": "Apple",
    "Apple": "Apple",
    "耐克": "Nike",
    "Nike": "Nike",
    "阿迪达斯": "Adidas",
    "Adidas": "Adidas",
}

_CATEGORY_ALIASES = {
    "数码": "数码电子",
    "电子": "数码电子",
    "手机": "数码电子",
    "耳机": "数码电子",
    "运动": "服饰运动",
    "服饰": "服饰运动",
    "衣服": "服饰运动",
    "美妆": "美妆护肤",
    "护肤": "美妆护肤",
    "食品": "食品生活",
    "家居": "食品生活",
}

_STYLE_ALIASES = {
    "轻一点": "轻量",
    "轻便": "轻量",
    "轻量": "轻量",
    "便携": "轻量",
    "通勤": "通勤",
    "性价比": "高性价比",
    "高性价比": "高性价比",
    "品质": "品质优先",
    "质感": "品质优先",
}


def extract_preference_updates(
    query: str,
    user_id: str | None,
    session_id: str | None,
) -> list[PreferenceUpdate]:
    if not user_id:
        return []
    text = (query or "").strip()
    if not text or _is_blocked(text):
        return []

    updates: list[PreferenceUpdate] = []
    updates.extend(_extract_budget(text, session_id))
    updates.extend(_extract_brand_exclude(text, session_id))
    updates.extend(_extract_brand_include(text, session_id))
    updates.extend(_extract_styles(text, session_id))
    updates.extend(_extract_categories(text, session_id))
    return _dedupe(updates)


def _is_blocked(text: str) -> bool:
    if any(marker in text for marker in _TEMPORARY_MARKERS):
        return True
    if any(marker in text for marker in _QUESTION_MARKERS):
        return True
    if any(marker in text for marker in _HIGH_RISK_MARKERS):
        return True
    return False


def _extract_budget(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    if not any(marker in text for marker in ("预算", "一般", "平时", "通常", "以后")):
        return []
    range_match = re.search(r"(\d{2,6})\s*(?:-|到|至|~|—)\s*(\d{2,6})", text)
    if range_match:
        low = float(range_match.group(1))
        high = float(range_match.group(2))
        if low > high:
            low, high = high, low
        return [
            PreferenceUpdate(
                field="budget_range",
                value={"budget_min": low, "budget_max": high},
                message=f"已记住：常用预算 ¥{int(low)}-¥{int(high)}",
                session_id=session_id,
            )
        ]
    max_match = re.search(r"(\d{2,6})\s*(?:以内|以下|左右)", text)
    if max_match and any(marker in text for marker in ("预算", "一般", "平时", "通常", "以后")):
        high = float(max_match.group(1))
        return [
            PreferenceUpdate(
                field="budget_max",
                value=high,
                message=f"已记住：常用预算 ¥{int(high)} 以内",
                session_id=session_id,
            )
        ]
    return []


def _extract_brand_exclude(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    if not any(marker in text for marker in ("不买", "不要", "不推荐", "避开", "排除", "不喜欢")):
        return []
    brands = [_canonical_brand(b) for b in _KNOWN_BRANDS if b in text]
    return [
        PreferenceUpdate(
            field="brand_exclude",
            value=brand,
            message=f"已记住：避免推荐 {brand}",
            session_id=session_id,
        )
        for brand in dict.fromkeys(brands)
    ]


def _extract_brand_include(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    if not any(marker in text for marker in ("我喜欢", "偏好", "优先", "以后推荐", "常买")):
        return []
    if any(marker in text for marker in ("不喜欢", "不要", "不买", "避开")):
        return []
    brands = [_canonical_brand(b) for b in _KNOWN_BRANDS if b in text]
    return [
        PreferenceUpdate(
            field="brand_include",
            value=brand,
            message=f"已记住：优先考虑 {brand}",
            session_id=session_id,
        )
        for brand in dict.fromkeys(brands)
    ]


def _extract_styles(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    if not any(marker in text for marker in ("以后", "平时", "一般", "偏好", "喜欢", "优先", "推荐")):
        return []
    styles = [canonical for marker, canonical in _STYLE_ALIASES.items() if marker in text]
    return [
        PreferenceUpdate(
            field="preference_keywords",
            value=style,
            message=f"已记住：偏好{style}",
            session_id=session_id,
        )
        for style in dict.fromkeys(styles)
    ]


def _extract_categories(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    if not any(marker in text for marker in ("主要看", "常买", "关注", "偏好", "喜欢")):
        return []
    categories = [canonical for marker, canonical in _CATEGORY_ALIASES.items() if marker in text]
    return [
        PreferenceUpdate(
            field="favorite_categories",
            value=category,
            message=f"已记住：关注{category}",
            session_id=session_id,
        )
        for category in dict.fromkeys(categories)
    ]


def _canonical_brand(brand: str) -> str:
    return _BRAND_CANONICAL.get(brand, brand)


def _dedupe(updates: list[PreferenceUpdate]) -> list[PreferenceUpdate]:
    seen: set[tuple[str, str]] = set()
    out: list[PreferenceUpdate] = []
    for update in updates:
        key = (update.field, str(update.value))
        if key in seen:
            continue
        seen.add(key)
        out.append(update)
    return out
