"""Evaluate intent-router accuracy across seven tool classes on an annotated dataset.

Overall accuracy, per-tool metrics, and a confusion matrix show which intents are stable or easily
confused in the function-calling router.

Run from `backend/`:
    python -m eval.routing_eval
    python -m eval.routing_eval --json out.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any

from eval._common import (
    BACKEND_DIR,  # noqa: F401  Trigger sys.path setup.
    hr,
    load_dataset,
    load_env,
    pct,
)


def run(export_json: str | None = None) -> dict[str, Any]:
    load_env()
    # Route httpx through curl when the local Python TLS stack cannot reach the endpoint.
    from eval._common import install_curl_transport_fallback
    install_curl_transport_fallback()

    from agent.intent_router import KNOWN_TOOLS, route
    from agent.session import AgentSession

    dataset = load_dataset("routing_labels.json")
    cases = dataset["cases"]
    tools = list(KNOWN_TOOLS)

    correct = 0
    rows: list[dict[str, Any]] = []
    # confusion[expected][predicted] = count
    confusion: dict[str, dict[str, int]] = {t: defaultdict(int) for t in tools}
    per_tool_total: dict[str, int] = defaultdict(int)
    per_tool_correct: dict[str, int] = defaultdict(int)
    predicted_total: dict[str, int] = defaultdict(int)

    print(hr())
    print(f"Intent-routing accuracy  |  annotated cases = {len(cases)}  |  tool classes = {len(tools)}")
    print(hr())
    print(f"{'qid':<8}{'expected':<16}{'predicted':<16}{'OK?':<6}query")
    print(hr("-"))

    for case in cases:
        query = case["query"]
        expected = case["expected"]
        last_hits = case.get("last_hits") or []

        session = AgentSession(session_id=f"eval_{case['qid']}")
        if last_hits:
            session.set("last_hits", last_hits)

        decision = route(query, session)
        predicted = decision.tool
        ok = predicted == expected
        correct += int(ok)

        per_tool_total[expected] += 1
        per_tool_correct[expected] += int(ok)
        predicted_total[predicted] += 1
        if expected in confusion:
            confusion[expected][predicted] += 1

        rows.append(
            {
                "qid": case["qid"],
                "query": query,
                "expected": expected,
                "predicted": predicted,
                "correct": ok,
                "confidence": decision.confidence,
            }
        )
        mark = "✓" if ok else "✗"
        print(f"{case['qid']:<8}{expected:<16}{predicted:<16}{mark:<6}{query}")

    accuracy = correct / len(cases) if cases else 0.0

    print(hr())
    print("Per-tool metrics (recall by true class; precision by predicted class)")
    print(hr("-"))
    print(f"{'tool':<16}{'Support':>8}{'Recall':>10}{'Precision':>12}")
    per_tool: dict[str, dict[str, float]] = {}
    for t in tools:
        total = per_tool_total.get(t, 0)
        rec = (per_tool_correct.get(t, 0) / total) if total else 0.0
        pred_n = predicted_total.get(t, 0)
        # Precision is the fraction of predictions for `t` whose expected class is also `t`.
        tp = confusion.get(t, {}).get(t, 0)
        prec = (tp / pred_n) if pred_n else 0.0
        per_tool[t] = {"support": total, "recall": rec, "precision": prec}
        if total or pred_n:
            print(f"{t:<16}{total:>8}{pct(rec):>10}{pct(prec):>12}")

    print(hr())
    print("Confusion matrix (rows=true, columns=predicted; only supported true classes are shown)")
    print(hr("-"))
    header = "true\\pred".ljust(16) + "".join(f"{t[:8]:>10}" for t in tools)
    print(header)
    for t in tools:
        if per_tool_total.get(t, 0) == 0:
            continue
        line = t.ljust(16)
        for p in tools:
            line += f"{confusion[t].get(p, 0):>10}"
        print(line)

    print(hr())
    print(f"Overall intent-routing accuracy = {pct(accuracy)}  ({correct}/{len(cases)})")
    print(hr())

    summary = {
        "accuracy": accuracy,
        "correct": correct,
        "total": len(cases),
        "per_tool": per_tool,
    }

    if export_json:
        with open(export_json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"Detailed results exported to: {export_json}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate intent-routing accuracy")
    parser.add_argument("--json", dest="export_json", default=None, help="Path for detailed JSON output")
    args = parser.parse_args()
    run(export_json=args.export_json)


if __name__ == "__main__":
    main()
