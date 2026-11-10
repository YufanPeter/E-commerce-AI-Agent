from __future__ import annotations

"""SKU 规格选择：购物车选规格、SKU 级对比、预算内默认规格共用的一套纯函数。

SKU 统一用 dict 表示：{"sku_id": str, "options": {维度: 取值}, "price": float}。
``ProductSku``（store 层 dataclass）用 ``sku_to_dict`` 转一下即可。
"""

import re
from collections import OrderedDict
from typing import Any

from i18n import is_english


def sku_to_dict(sku: Any) -> dict[str, Any]:
    if isinstance(sku, dict):
        return {
            "sku_id": sku.get("sku_id"),
            "options": dict(sku.get("options") or sku.get("properties") or {}),
            "price": float(sku.get("price", 0) or 0),
        }
    return {
        "sku_id": getattr(sku, "sku_id", None),
        "options": dict(getattr(sku, "properties", None) or {}),
        "price": float(getattr(sku, "price", 0) or 0),
    }


def varying_dims(skus: list[dict[str, Any]]) -> "OrderedDict[str, list[str]]":
    """提取在给定 SKU 集合里取值多于一个的维度（颜色/尺码/容量…）。

    纯数字型维度（尺码/容量）按数值升序排列，便于阅读；其它维度保持出现顺序。"""
    dim_values: "OrderedDict[str, list[str]]" = OrderedDict()
    for sku in skus:
        for key, val in (sku.get("options") or {}).items():
            dim_values.setdefault(key, [])
            if val not in dim_values[key]:
                dim_values[key].append(val)
    return OrderedDict((k, sort_values(vs)) for k, vs in dim_values.items() if len(vs) > 1)


def all_dims(skus: list[dict[str, Any]]) -> "OrderedDict[str, list[str]]":
    """所有维度（含单值），保持出现顺序。"""
    dim_values: "OrderedDict[str, list[str]]" = OrderedDict()
    for sku in skus:
        for key, val in (sku.get("options") or {}).items():
            dim_values.setdefault(key, [])
            if val not in dim_values[key]:
                dim_values[key].append(val)
    return dim_values


def sort_values(values: list[str]) -> list[str]:
    """若每个取值都含数字（尺码/容量），按数字升序；否则保持原顺序。"""
    keyed: list[tuple[int, str]] = []
    for v in values:
        match = re.search(r"\d+", v)
        if not match:
            return values
        keyed.append((int(match.group()), v))
    return [v for _, v in sorted(keyed, key=lambda x: x[0])]


# 色系词根：库里颜色值五花八门（星芒黑 / 深空黑 / 黑色 Black），用户只说「黑色」「白的」。
COLOR_STEMS: tuple[str, ...] = (
    "藏青", "藏蓝", "海军蓝", "深蓝", "天蓝", "浅灰", "深灰", "米白", "象牙白",
    "黑", "白", "红", "蓝", "绿", "粉", "紫", "灰", "金", "银", "橙", "黄", "棕", "青", "米",
)
_COLOR_QUERY_RE = re.compile("(" + "|".join(COLOR_STEMS) + r")(?:色|的|款|那|这|系)")


def color_stems_in(query: str) -> list[str]:
    """从用户话里抽色系词根：「黑色那个」→ ['黑']，「藏青的」→ ['藏青']。要求后面跟
    色/的/款/那/这，避免「金」「米」这类单字在品牌名里误触（如「小米」）。"""
    found: list[str] = []
    for match in _COLOR_QUERY_RE.finditer(query or ""):
        stem = match.group(1)
        if stem not in found:
            found.append(stem)
    return found


def pick_color_value(values: list[str], query: str) -> str | None:
    """颜色维度专用：按色系词根挑唯一取值（「白色的」→「雪域白」）；多个同色系取值则不确定。"""
    stems = color_stems_in(query)
    if not stems:
        return None
    hits = [v for v in values if any(stem in str(v) for stem in stems)]
    return hits[0] if len(hits) == 1 else None


def _pick_for_dim(dim: str, values: list[str], query: str) -> str | None:
    chosen = pick_dim_value(values, query)
    if chosen is None and "色" in str(dim):
        chosen = pick_color_value(values, query)
    return chosen


def filter_skus(query: str, skus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按用户这句话里能识别到的规格值过滤 SKU（覆盖所有品类）。

    没识别到任何规格 → 原样返回；识别到几个维度 → 取交集，返回满足全部约束的 SKU。
    两段式匹配，确定性优先：① 精确整值命中（取最长）；② 模糊打分回退（唯一最高分）。"""
    dims = varying_dims(skus)
    constraints: dict[str, str] = {}
    for dim, values in dims.items():
        chosen = _pick_for_dim(dim, values, query)
        if chosen is not None:
            constraints[dim] = chosen
    if not constraints:
        return list(skus)
    return [
        sku for sku in skus
        if all((sku.get("options") or {}).get(d) == v for d, v in constraints.items())
    ]


def mentioned_constraints(query: str, skus: list[dict[str, Any]]) -> dict[str, str]:
    """这句话点到了哪些规格维度的取值（不过滤，只报告）。"""
    out: dict[str, str] = {}
    for dim, values in varying_dims(skus).items():
        chosen = _pick_for_dim(dim, values, query)
        if chosen is not None:
            out[dim] = chosen
    return out


_ENGLISH_SIZE_WORDS = (
    (re.compile(r"\b(?:double|2)\s*-?\s*(?:xl|extra[- ]large)\b", re.I), "xxl"),
    (re.compile(r"\b(?:extra|x)[- ]large\b", re.I), "xl"),
    (re.compile(r"\b(?:extra|x)[- ]small\b", re.I), "xs"),
    (re.compile(r"\blarge\b", re.I), "l"),
    (re.compile(r"\bmedium\b", re.I), "m"),
    (re.compile(r"\bsmall\b", re.I), "s"),
)
_ENGLISH_UNITS = (
    (re.compile(r"\b(\d+)\s*(?:ft|feet|foot)\b", re.I), r"\1 ft"),
    (re.compile(r"\b(\d+)\s*(?:in|inch|inches)\b", re.I), r"\1 in"),
    (re.compile(r"\b(\d+)\s*gb\b", re.I), r"\1gb"),
    (re.compile(r"\bgrey\b", re.I), "gray"),
)


def _english_value_text(text: str, *, query: bool) -> str:
    """Canonical form so spoken and catalog spellings compare equal."""
    result = text.casefold()
    for pattern, replacement in _ENGLISH_UNITS:
        result = pattern.sub(replacement, result)
    if query:
        for pattern, replacement in _ENGLISH_SIZE_WORDS:
            result = pattern.sub(replacement, result)
    return result


def _pick_english_value(values: list[str], query: str) -> str | None:
    text = _english_value_text(query, query=True)
    hits = [
        value for value in values
        if value and re.search(rf"(?<![\w'’])(?<!\d\.){re.escape(_english_value_text(value, query=False))}(?!\w|\.\d)", text)
    ]
    if not hits:
        return None
    hits.sort(key=len, reverse=True)
    return hits[0] if len(hits) == 1 or len(hits[0]) > len(hits[1]) else None


def pick_dim_value(values: list[str], query: str) -> str | None:
    """在某维度的候选取值里，选出用户这句话指定的那个；选不出返回 None。"""
    if is_english():
        return _pick_english_value(values, query)
    q = query or ""
    q_lower = q.lower()
    exact = [v for v in values if v and (v in q or v.lower() in q_lower)]
    if exact:
        exact.sort(key=len, reverse=True)
        if len(exact) == 1 or len(exact[0]) > len(exact[1]):
            return exact[0]
        return None
    best_val: str | None = None
    best_score = 0
    tie = False
    for val in values:
        score = value_match_score(val, q)
        if score > best_score:
            best_score, best_val, tie = score, val, False
        elif score == best_score and score > 0:
            tie = True
    if best_val is not None and best_score > 0 and not tie:
        return best_val
    return None


def value_match_score(value: str, query: str) -> int:
    """用户话里命中了该规格取值的多少个 token（数字/中文片段/英文整词）。"""
    value = (value or "").strip()
    if not value:
        return 0
    q = (query or "").lower()
    score = 0
    size_match = re.fullmatch(r"([A-Za-z]{1,3})(码)?", value)
    if size_match:
        token = value.lower()
        right_guard = "" if size_match.group(2) else r"(?![a-z])"
        if re.search(rf"(?<![a-z]){re.escape(token)}{right_guard}", q):
            score += 1
        return score
    for num in re.findall(r"\d+", value):
        if re.search(rf"(?<!\d){re.escape(num)}(?!\d)", query or ""):
            score += 1
    for run in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(run) >= 2 and run in (query or ""):
            score += 1
        elif len(run) >= 3:
            for i in range(len(run) - 1):
                if run[i:i + 2] in (query or ""):
                    score += 1
                    break
    for word in re.findall(r"[a-zA-Z]{3,}", value):
        if word.lower() in q:
            score += 1
    return score


def sku_label(sku: dict[str, Any] | None) -> str:
    """规格的一行展示：「12GB+256GB · 星芒黑」。"""
    if not sku:
        return ""
    return " · ".join(str(v) for v in (sku.get("options") or {}).values() if str(v).strip())


def select_sku(
    skus: list[dict[str, Any]],
    *,
    query: str = "",
    sku_id: str | None = None,
    budget_max: float | None = None,
    budget_min: float | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """给对比 / 默认规格选一个确定的 SKU，返回 (sku, 依据)。

    优先级：显式 sku_id → 话里点到的规格（多个命中取最便宜）→ 预算内最便宜 → 最便宜。
    """
    if not skus:
        return None, "none"
    by_price = sorted(skus, key=lambda s: (float(s.get("price", 0) or 0), str(s.get("sku_id"))))
    if sku_id:
        for sku in skus:
            if str(sku.get("sku_id")) == str(sku_id):
                return sku, "explicit"
    if query:
        matched = filter_skus(query, skus)
        if matched and len(matched) < len(skus):
            matched = sorted(matched, key=lambda s: float(s.get("price", 0) or 0))
            return matched[0], "query"
    if budget_max is not None or budget_min is not None:
        within = [
            s for s in by_price
            if (budget_max is None or float(s.get("price", 0) or 0) <= budget_max)
            and (budget_min is None or float(s.get("price", 0) or 0) >= budget_min)
        ]
        if within:
            return within[0], "budget"
    return by_price[0], "cheapest"
