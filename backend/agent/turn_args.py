"""Structured arguments for the tool a turn was routed to.

English turns take them from the router, which reads the message against what is on
screen. The Chinese demo reads them from the shopper's own words with the rules below.
"""

from __future__ import annotations

import re
from typing import Any

from agent.intent_router import IntentDecision
from agent.session import AgentSession
from agent.tools.reference import resolve_indices
from search.catalog_understanding import names_withdrawal, narrow_clears
from search.query_understanding import ParsedQuery


# 「第二个」是序号不是数量；「第」后面的数字不算。
_QUANTITY_RE = re.compile(r"(?<![第0-9])([0-9]+|[一两二三四五六七八九十])\s*(瓶|件|个|盒|箱|袋|桶|双|条|支|份|罐|包|台|部)")
_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NO_BUDGET_ZH = re.compile(r"预算不限|不限预算|价格不限|不限价格|不设预算|多少钱都行")
_ANY_BRAND_ZH = re.compile(r"品牌不限|不限品牌|什么牌子都行|哪个牌子都行|牌子无所谓|品牌无所谓")
_SPEC_CHANGE_ZH = re.compile(r"换成|改成|换个|换一个|大一[码号]|小一[码号]")
_SIZE_UP_ZH = re.compile(r"大一[码号]")
_SIZE_DOWN_ZH = re.compile(r"小一[码号]")


def quantity_in(text: str) -> int | None:
    match = _QUANTITY_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1)
    return int(raw) if raw.isdigit() else _CN_DIGITS.get(raw)


def cleared_constraints(text: str) -> list[str]:
    """Constraints the shopper withdrew: any of "budget" and "brand"."""
    return [
        name for name, pattern in (("budget", _NO_BUDGET_ZH), ("brand", _ANY_BRAND_ZH))
        if pattern.search(text or "")
    ]


def spec_change_cue(text: str) -> bool:
    return _SPEC_CHANGE_ZH.search(text or "") is not None


def size_step(text: str) -> int:
    """+1 for "one size up", -1 for "one size down", otherwise 0."""
    if _SIZE_UP_ZH.search(text or ""):
        return 1
    return -1 if _SIZE_DOWN_ZH.search(text or "") else 0


def _focus_index(items: list[dict[str, Any]], session: AgentSession) -> int | None:
    return next((i for i, item in enumerate(items) if item.get("product_id") == session.task.focus_product_id), None)


def tool_slots(tool: str, raw_query: str, session: AgentSession) -> dict[str, Any]:
    if not raw_query:
        return {}
    slots: dict[str, Any] = {}
    if tool in {"cart", "product_detail", "similar", "accessories"}:
        task = session.task
        items = task.add_targets() if tool == "cart" else task.active_view.items
        slots["target"] = {
            "indices": resolve_indices(raw_query, len(items), focus_index=_focus_index(items, session)),
            "view_version": task.active_view.version,
        }
    if tool == "cart":
        slots["quantity"] = quantity_in(raw_query)
        slots["spec_update"] = {"step": size_step(raw_query)}
    if tool in {"recommend", "refine"}:
        clear = cleared_constraints(raw_query)
        if clear:
            slots["constraint_ops"] = {"clear": clear}
    return slots


def _carried_search(session: AgentSession) -> ParsedQuery | None:
    last = session.recall_parsed()
    try:
        return ParsedQuery.from_dict(last) if isinstance(last, dict) else None
    except TypeError:
        return None


def routed_slots(decision: IntentDecision, session: AgentSession, raw_query: str = "") -> dict[str, Any]:
    """Slots from the router's arguments, checked against what is on screen and what was said."""
    args = decision.args or {}
    tool = decision.tool
    task = session.task
    items = task.add_targets() if tool == "cart" else task.active_view.items
    indices = [p - 1 for p in args.get("positions") or [] if 0 < p <= len(items)]
    if tool in {"cart", "product_detail", "similar", "accessories"} and not indices:
        # A reference the router could not place falls back to the focused product.
        focus = _focus_index(items, session)
        indices = [focus] if focus is not None and tool != "cart" else []
    slots: dict[str, Any] = {}
    if tool in {"cart", "product_detail", "similar", "accessories"}:
        slots["target"] = {"indices": indices, "view_version": task.active_view.version}
    if tool == "compare" and len(indices) >= 2:
        slots["product_ids"] = [items[i]["product_id"] for i in indices]
    if tool == "cart":
        slots.update(
            cart_action=args.get("cart_action") or "view",
            cart_item=args.get("cart_item"),
            quantity=args.get("quantity"),
            spec_update={"step": args.get("size_step", 0)},
            whole_order=bool(args.get("whole_order")),
            reorder_item=args.get("reorder_item") or "",
        )
    if tool in {"recommend", "refine"}:
        base = _carried_search(session)
        # The router's clear list is a reading, checked against the shopper's own words.
        clear = narrow_clears(raw_query, base, args.get("clear") or []) if raw_query else list(args.get("clear") or [])
        # A withdrawn condition also needs the tool to read the raw message, not a rewrite.
        if clear or (tool == "refine" and names_withdrawal(raw_query, base)):
            slots["constraint_ops"] = {"clear": clear}
    if args.get("cheaper"):
        slots["cheaper"] = True
    return slots
