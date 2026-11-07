from __future__ import annotations

"""从行为事件推断偏好建议。

推断结果只是建议：写进台账的 suggested 状态，用户确认前不改偏好，不做硬过滤。
未确认的品牌建议只给排序一个很小的加权，并在话术里说明。

规则都是可解释的计数，页面能直接展示依据（「最近加购 2 次」），用户才判断得了对错。
事件按半衰期衰减，很久以前的行为不再起作用。
"""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from search.query_understanding import BRAND_ALIASES
from store.user_memory_store import MemoryKey


HALF_LIFE_DAYS = 30.0
_BRAND_WEIGHTS = {"checkout": 5.0, "add_cart": 3.0, "view_detail": 1.0, "compare": 1.0}
_BRAND_MIN_SCORE = 4.0
_BRAND_MIN_EVENTS = 2
_BUDGET_MIN_SEARCHES = 3
_CATEGORY_MIN_SEARCHES = 4
_VARIANT_TO_CANONICAL = {v: canonical for canonical, variants in BRAND_ALIASES.items() for v in variants}
_EVENT_LABELS = {"checkout": "下单", "add_cart": "加购", "view_detail": "看详情", "compare": "对比"}


@dataclass(frozen=True)
class Suggestion:
    key: MemoryKey
    evidence: str
    confidence: float


def _decay(created_at: str | None, now: datetime) -> float:
    try:
        created = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 1.0
    age_days = max(0.0, (now - created).total_seconds() / 86400)
    return math.pow(0.5, age_days / HALF_LIFE_DAYS)


def infer_suggestions(events: list[dict[str, Any]], now: datetime | None = None) -> list[Suggestion]:
    now = now or datetime.now(timezone.utc)
    suggestions = _infer_brands(events, now)
    suggestions += _infer_budgets(events)
    suggestions += _infer_categories(events)
    return suggestions


def _infer_brands(events: list[dict[str, Any]], now: datetime) -> list[Suggestion]:
    scores: dict[str, float] = defaultdict(float)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in events:
        weight = _BRAND_WEIGHTS.get(event.get("type"))
        brand = event.get("brand")
        if not weight or not brand:
            continue
        canonical = _VARIANT_TO_CANONICAL.get(brand, brand)
        scores[canonical] += weight * _decay(event.get("created_at"), now)
        counts[canonical][event["type"]] += 1
    out = []
    for brand, score in scores.items():
        if score < _BRAND_MIN_SCORE or sum(counts[brand].values()) < _BRAND_MIN_EVENTS:
            continue
        parts = [f"{_EVENT_LABELS[kind]} {n} 次" for kind, n in counts[brand].items()]
        out.append(Suggestion(
            key=MemoryKey("brand_like", value=brand),
            evidence="最近" + "、".join(parts),
            confidence=min(0.9, 0.4 + score / 20),
        ))
    return out


def _infer_budgets(events: list[dict[str, Any]]) -> list[Suggestion]:
    by_scope: dict[str, list[float]] = defaultdict(list)
    for event in events:
        if event.get("type") != "search":
            continue
        payload = event.get("payload") or {}
        scope = payload.get("sub_category")
        price = payload.get("max_price")
        if scope and price:
            by_scope[str(scope)].append(float(price))
    out = []
    for scope, prices in by_scope.items():
        if len(prices) < _BUDGET_MIN_SEARCHES:
            continue
        median = statistics.median(prices)
        # 价格说得很散就不算习惯
        if max(prices) > median * 1.5 or min(prices) < median * 0.5:
            continue
        out.append(Suggestion(
            key=MemoryKey("budget", scope=scope, value=f"-{median:g}"),
            evidence=f"最近 {len(prices)} 次找{scope}，预算都在 ¥{median:g} 上下",
            confidence=min(0.85, 0.4 + 0.1 * len(prices)),
        ))
    return out


def _infer_categories(events: list[dict[str, Any]]) -> list[Suggestion]:
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for event in events:
        if event.get("type") != "search":
            continue
        category = (event.get("payload") or {}).get("category")
        if category:
            counts[str(category)] += 1
            total += 1
    return [
        Suggestion(
            key=MemoryKey("category", value=category),
            evidence=f"最近 {total} 次搜索里有 {n} 次在看{category}",
            confidence=min(0.8, n / total),
        )
        for category, n in counts.items()
        if n >= _CATEGORY_MIN_SEARCHES and n / total >= 0.5
    ]
