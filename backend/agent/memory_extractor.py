from __future__ import annotations

"""确定性偏好抽取：第一道关，只处理明确、低风险的长期陈述。

忽略一次性的话（「推荐通勤的双肩包」「预算 500 的耳机」）、替别人买的话，以及模糊说法。
更口语的表达由 ``agent.memory_llm`` 补充；健康相关的说法（过敏、忌口）在这里只抽成
待确认建议（``extract_sensitive_terms``），用户点确认才生效。
"""

import dataclasses
import re
from typing import Any

from search.query_understanding import _SUBCAT_SYNONYMS, load_taxonomy
from store.user_memory_store import PreferenceUpdate


_TEMPORARY_MARKERS = ("这次", "今天", "先", "暂时", "试试", "看看", "当前", "本次", "这回", "这一次")
# 带「先」但并不表示临时的词，判断前先去掉，否则「以后优先推荐」永远写不进来。
_NOT_TEMPORARY_WORDS = ("优先", "首先", "事先", "原先", "预先")
# 替别人买：说的是别人的情况，不能记到用户自己头上。
_THIRD_PARTY_MARKERS = (
    "给我妈", "给我爸", "我妈", "我爸", "爸爸", "妈妈", "老婆", "老公", "女朋友", "男朋友", "对象",
    "孩子", "儿子", "女儿", "朋友", "同事", "室友", "长辈", "送人", "送给", "她",
    "给他", "帮他", "他是", "他穿", "他用", "他的",  # 不能只写「他」：「其他品牌」也含这个字
)
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
    "食品": "食品饮料",
    "零食": "食品饮料",
    "饮料": "食品饮料",
}

# 肤质：统一成页面展示用的说法
_SKIN_TYPES = {
    "油皮": "油皮", "油性": "油皮", "大油皮": "油皮",
    "干皮": "干皮", "干性": "干皮", "干敏": "干敏肌",
    "混油": "混油皮", "混干": "混干皮", "混合性": "混合肌", "混合皮": "混合肌",
    "敏感肌": "敏感肌", "敏感皮": "敏感肌", "中性": "中性皮",
}
# 必须是在说自己（「我是」「我这种」），「我要买适合油皮的」不算。
_SKIN_PATTERN = re.compile(r"(?:我是|我属于|本人是|我这种|我这个|我这|肤质是|肤质为|肤质|皮肤是|我皮肤|我的皮肤)[^，。,]{0,4}?(大油皮|油皮|油性|干敏|干皮|干性|混油|混干|混合性|混合皮|敏感肌|敏感皮|中性)")
_SKIN_PLAIN_PATTERN = re.compile(r"(?:我|我的)?皮肤(?:比较|偏|很|挺|特别|有点)(干|油|敏感)")
_SHOE_PATTERN = re.compile(r"(?:我穿|平时穿|一般穿|通常穿|都穿|鞋码|脚码|我的码)[^0-9]{0,6}(3[5-9]|4[0-8])(?:\.5)?\s*(?:码|号)?")
_CLOTH_PATTERN = re.compile(r"(?:我穿|平时穿|一般穿|通常穿|都穿|衣服穿|上衣穿)[^A-Za-z]{0,4}(XXXL|XXL|XL|XS|S|M|L)\s*(?:码|号)?", re.IGNORECASE)
_SENSITIVE_PATTERNS = (
    re.compile(r"对([^，。,、！!？?\s]{1,8}?)过敏"),
    re.compile(r"([^，。,、！!？?\s我是对]{1,6}?)过敏"),
    re.compile(r"(?:不能吃|不能喝|不能用|吃不了|喝不了|忌口)([^，。,、！!？?\s的了]{1,6})"),
)
_DURABLE_BUDGET_MARKERS = ("一般", "平时", "通常", "以后", "一直", "常用", "每次")


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
    if not text or _is_third_party(text):
        return []

    updates: list[PreferenceUpdate] = []
    # 「我是油皮，用什么洗面奶好」是问句，但肤质本身是长期事实，不受问句限制。
    updates.extend(_extract_attributes(text, session_id))
    if not _is_blocked(text):
        updates.extend(_extract_budget(text, session_id))
        updates.extend(_extract_brand_exclude(text, session_id))
        updates.extend(_extract_brand_include(text, session_id))
        updates.extend(_extract_styles(text, session_id))
        updates.extend(_extract_categories(text, session_id))
    return [dataclasses.replace(update, evidence=text) for update in _dedupe(updates)]


def extract_sensitive_terms(query: str) -> list[str]:
    """过敏 / 忌口：只抽出来待用户确认，不能自动变成硬过滤。"""
    text = (query or "").strip()
    if not text or _is_third_party(text):
        return []
    terms: list[str] = []
    for pattern in _SENSITIVE_PATTERNS:
        for match in pattern.finditer(text):
            term = match.group(1).strip("的了吗呢啊")
            if len(term) >= 1 and term not in terms and not any(term in t or t in term for t in terms):
                terms.append(term)
    return terms


def _is_third_party(text: str) -> bool:
    return any(marker in text for marker in _THIRD_PARTY_MARKERS)


def _extract_attributes(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    updates: list[PreferenceUpdate] = []
    skin = None
    match = _SKIN_PATTERN.search(text)
    if match:
        skin = _SKIN_TYPES.get(match.group(1))
    else:
        plain = _SKIN_PLAIN_PATTERN.search(text)
        if plain:
            skin = {"干": "干皮", "油": "油皮", "敏感": "敏感肌"}[plain.group(1)]
    if skin:
        updates.append(_attr_update("美妆护肤", "skin_type", skin, f"已记住：肤质是{skin}", session_id))
    shoe = _SHOE_PATTERN.search(text)
    if shoe:
        size = f"{shoe.group(1)}码"
        updates.append(_attr_update("服饰运动", "shoe_size", size, f"已记住：鞋码 {size}", session_id))
    cloth = _CLOTH_PATTERN.search(text)
    if cloth:
        size = f"{cloth.group(1).upper()}码"
        updates.append(_attr_update("服饰运动", "clothing_size", size, f"已记住：衣服穿 {size}", session_id))
    return updates


def _attr_update(category: str, key: str, value: str, message: str, session_id: str | None) -> PreferenceUpdate:
    return PreferenceUpdate(
        field="category_attr",
        value={"category": category, "key": key, "value": value},
        message=message,
        session_id=session_id,
    )


def _known_brands() -> list[str]:
    """静态常用品牌 + 商品库里的全部品牌。"""
    brands = list(_KNOWN_BRANDS)
    try:
        for brand in load_taxonomy().get("brands", []):
            if brand not in brands and " " not in brand:
                brands.append(brand)
    except Exception:  # noqa: BLE001 - 取不到商品库时用静态表
        pass
    return brands


def _budget_scope(text: str) -> str | None:
    """「买手机一般 3000 以内」的预算只对手机成立。"""
    for word in sorted(_SUBCAT_SYNONYMS, key=len, reverse=True):
        if word in text:
            return _SUBCAT_SYNONYMS[word]
    return None


def _is_blocked(text: str) -> bool:
    checked = text
    for word in _NOT_TEMPORARY_WORDS:
        checked = checked.replace(word, "")
    if any(marker in checked for marker in _TEMPORARY_MARKERS):
        return True
    if any(marker in text for marker in _QUESTION_MARKERS):
        return True
    if any(marker in text for marker in _HIGH_RISK_MARKERS):
        return True
    return False


def _extract_budget(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    # 「预算 500 的耳机」只是这一次的约束，要有长期语气才记。
    if not any(marker in text for marker in _DURABLE_BUDGET_MARKERS):
        return []
    scope = _budget_scope(text)
    label = f"{scope}预算" if scope else "常用预算"
    range_match = re.search(r"(\d{2,6})\s*(?:-|到|至|~|—)\s*(\d{2,6})", text)
    if range_match:
        low = float(range_match.group(1))
        high = float(range_match.group(2))
        if low > high:
            low, high = high, low
        message = f"已记住：{label} ¥{int(low)}-¥{int(high)}"
        if scope:
            return [PreferenceUpdate(
                field="category_budget",
                value={"category": scope, "budget_min": low, "budget_max": high},
                message=message,
                session_id=session_id,
            )]
        return [PreferenceUpdate(
            field="budget_range",
            value={"budget_min": low, "budget_max": high},
            message=message,
            session_id=session_id,
        )]
    max_match = re.search(r"(\d{2,6})\s*(?:块|元)?\s*(?:以内|以下|左右)", text)
    if max_match:
        high = float(max_match.group(1))
        message = f"已记住：{label} ¥{int(high)} 以内"
        if scope:
            return [PreferenceUpdate(
                field="category_budget",
                value={"category": scope, "budget_min": None, "budget_max": high},
                message=message,
                session_id=session_id,
            )]
        return [PreferenceUpdate(field="budget_max", value=high, message=message, session_id=session_id)]
    return []


# 「长期」语气词：只有带这些的否定才写进跨会话记忆。
# 「不要苹果」单独说一次只是本轮/本会话的检索约束（ParsedQuery.brand_exclude 已经处理了），
# 不能变成以后每次搜索都硬排除苹果；「以后都不要苹果」「我一直不买苹果」才是长期偏好。
_DURABLE_MARKERS = (
    "以后", "一直", "都不", "从来", "永远", "再也", "一般不", "平时不", "向来", "历来",
    "别再", "不喜欢", "讨厌", "从不",
)


def _is_durable_statement(text: str) -> bool:
    return any(marker in text for marker in _DURABLE_MARKERS)


def _extract_brand_exclude(text: str, session_id: str | None) -> list[PreferenceUpdate]:
    if not any(marker in text for marker in ("不买", "不要", "不推荐", "避开", "排除", "不喜欢", "别推", "别再", "讨厌", "不用")):
        return []
    if not _is_durable_statement(text):
        return []
    brands = [_canonical_brand(b) for b in _known_brands() if b in text]
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
    if not any(marker in text for marker in ("我喜欢", "偏好", "优先", "以后推荐", "常买", "一直用", "习惯用", "我用惯", "只买")):
        return []
    if any(marker in text for marker in ("不喜欢", "不要", "不买", "避开")):
        return []
    brands = [_canonical_brand(b) for b in _known_brands() if b in text]
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
    # 单说「推荐通勤的双肩包」只是这一次的需求，不是长期风格。
    if not any(marker in text for marker in ("以后", "平时", "一般", "偏好", "喜欢", "优先", "一直", "习惯")):
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
    if not any(marker in text for marker in ("主要看", "常买", "关注", "经常看", "经常买", "喜欢看", "喜欢买")):
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
