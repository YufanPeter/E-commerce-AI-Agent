"""Score English turn routing on the Amazon catalog: the tool and the arguments it acts on.

Each case builds a conversation state (results on screen, focus, cart, a waiting
question), then asks the orchestrator how it would handle the message. No tool runs.

    CARTPILOT_CATALOG=amazon python -m eval.routing_eval_en [--out report.json]
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Callable

from eval._common import load_dataset, load_env

FIELDS = ("tool", "positions", "cart_action", "cart_item", "quantity", "size_step", "clear", "cheaper", "whole_order")


def build_session(state: dict[str, Any], fixtures: dict[str, Any]) -> Any:
    from agent.session import AgentSession

    session = AgentSession()
    for role, text in state.get("history", []):
        (session.add_user if role == "user" else session.add_assistant)(text)
    items = [{"product_id": pid, "title": title} for pid, title in fixtures["speakers"]]
    tshirt_id, tshirt_title = fixtures["tshirt"]
    if state.get("list"):
        if not state.get("history"):
            session.add_user("Bluetooth speakers")
        session.add_assistant("Here are five Bluetooth speakers: " + "; ".join(i["title"] for i in items))
        session.remember_search({"original_query": "Bluetooth speakers", "sub_category": "Bluetooth Speakers"}, items)
    if state.get("focus"):
        session.task.focus_on(items[state["focus"] - 1]["product_id"])
    if state.get("cart_view"):
        session.task.show("cart", [{"product_id": pid, "title": title} for pid, title in fixtures["cart"]])
        session.add_assistant("Your cart: 1. " + fixtures["cart"][0][1] + " 2. " + fixtures["cart"][1][1])
    if state.get("last_added"):
        session.task.added_to_cart(tshirt_id, "sim_eval_sku")
        session.add_assistant(f"Added {tshirt_title} (S, White) to your cart.")
    if state.get("pending") == "cart_sku":
        session.task.ask("cart_sku", {"product_id": tshirt_id, "title": tshirt_title, "quantity": 1, "selected": {}, "resolution": {}})
        session.add_assistant(f"Which size and color of {tshirt_title}? Sizes XS-XXL; colors White, Beige, Gray.")
    if state.get("pending") == "cart_pick":
        session.task.ask("cart_pick", {"candidates": items[:2], "quantity": 1})
        session.add_assistant(f"Which one should I add? 1. {items[0]['title']} 2. {items[1]['title']}")
    return session


def _passes(field: str, expected: Any, seen: dict[str, Any]) -> bool:
    if field == "tool":
        return seen["tool"] in expected
    if field == "clear":
        return sorted(seen.get("clear") or []) == sorted(expected)
    if field in ("cheaper", "whole_order"):
        return bool(seen.get(field)) == expected
    if field == "size_step":
        return int(seen.get("size_step") or 0) == expected
    return seen.get(field) == expected


def run(decide: Callable[[str, Any], dict[str, Any]]) -> dict[str, Any]:
    data = load_dataset("amazon_routing_cases.json")
    totals, failures = Counter(), []
    for case in data["cases"]:
        session = build_session(case["state"], data["fixtures"])
        session.add_user(case["message"])
        seen = decide(case["message"], session)
        wrong = [f for f in FIELDS if f in case["expect"] and not _passes(f, case["expect"][f], seen)]
        for field in case["expect"]:
            totals[(field, field not in wrong)] += 1
        totals[("case", not wrong)] += 1
        if wrong:
            failures.append({"id": case["id"], "message": case["message"], "wrong": wrong,
                             "expected": {f: case["expect"][f] for f in wrong}, "seen": seen})
    by_field = {
        field: f"{totals[(field, True)]}/{totals[(field, True)] + totals[(field, False)]}"
        for field in ("case", *FIELDS) if totals[(field, True)] + totals[(field, False)]
    }
    return {"by_field": by_field, "failures": failures}


def decide_with_orchestrator(orchestrator: Any) -> Callable[[str, Any], dict[str, Any]]:
    """Normalize the orchestrator's decision and slots into the fields this eval checks."""

    def decide(message: str, session: Any) -> dict[str, Any]:
        decision, slots = orchestrator.decide(message, session, {"timings": {}})
        items = session.task.add_targets() if decision.tool == "cart" else session.task.active_view.items
        if "product_ids" in slots:
            ids = [i["product_id"] for i in items]
            positions = [ids.index(p) + 1 for p in slots["product_ids"] if p in ids]
        else:
            positions = [i + 1 for i in (slots.get("target") or {}).get("indices", [])]
        return {
            "tool": decision.tool,
            "positions": positions,
            "cart_action": slots.get("cart_action"),
            "cart_item": slots.get("cart_item"),
            "quantity": slots.get("quantity"),
            "size_step": (slots.get("spec_update") or {}).get("step", 0),
            "clear": (slots.get("constraint_ops") or {}).get("clear", []),
            "cheaper": slots.get("cheaper", False),
            "whole_order": slots.get("whole_order", False),
        }

    return decide


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Write the full report as JSON.")
    args = parser.parse_args()
    load_env()
    from agent.orchestrator import Agent as Orchestrator

    report = run(decide_with_orchestrator(Orchestrator()))
    print(json.dumps(report["by_field"], indent=1))
    for failure in report["failures"]:
        seen = {k: failure["seen"].get(k) for k in failure["wrong"]}
        print(f"- {failure['id']}: {failure['message']!r} expected={failure['expected']} seen={seen}")
    if args.out:
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
