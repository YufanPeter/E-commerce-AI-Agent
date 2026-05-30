"""手动验证 clarify → recommend 多轮闭环（流式）。

跑法：
    cd backend && python -m agent.tests.manual_clarify_loop
"""
from __future__ import annotations

import os
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from agent.orchestrator import Agent
from agent.session import AgentSession


def main() -> None:
    agent = Agent()
    session = AgentSession()

    turns = [
        "随便看看",         # → 应该 clarify
        "美妆，500 以内",   # → 应该 recommend（融合上下文）
    ]
    for q in turns:
        print(f"\n>>> 用户: {q}")
        narrative = []
        for ev in agent.handle_turn_stream(q, session):
            t = ev["type"]
            if t == "meta":
                d = ev["data"]["decision"]
                print(f"    [router] tool={d['tool']}  rewritten={d['rewritten_query']!r}")
                print(f"    [router] reason: {d['reasoning']}")
            elif t == "tool_result":
                products = (ev["data"].get("payload") or {}).get("products") or []
                if products:
                    print(f"    [tool] {len(products)} hits")
            elif t == "token":
                narrative.append(ev["data"])
            elif t == "done":
                print(f"    [timings] {ev['data']['timings']}")
        print(f"    助手: {''.join(narrative)[:240]}")


if __name__ == "__main__":
    main()
