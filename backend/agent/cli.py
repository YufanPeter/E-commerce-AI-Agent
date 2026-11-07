from __future__ import annotations

"""Interactive streaming CLI demo.

Usage:
    cd backend && python -m agent.cli
    > 推荐 500 以内的敏感肌精华
    > /exit

Environment variables:
    LOG_LEVEL=INFO       Enable internal logs.
    AGENT_STREAM=0       Force blocking mode (streaming is enabled by default).
"""

import json
import logging
import os
import sys
import time

# --- These environment settings must precede chromadb/sentence_transformers imports. ---
# 1. Disable Chroma's PostHog telemetry to avoid harmless but noisy console errors.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_IMPL", "none")
# 2. Use the local Hugging Face cache without checking for model updates at startup.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from agent.orchestrator import Agent
from agent.session import AgentSession


def _warmup() -> None:
    """Synchronously initialize the LLM client and search service."""
    t0 = time.perf_counter()
    print("  Loading the LLM client...", end="", flush=True)
    from llm.client import get_client
    get_client()
    print(f" {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    print("  Loading Chroma and the embedding model...", end="", flush=True)
    from search.search_service import get_search_service
    svc = get_search_service()
    svc.search("预热", top_k_chunks=3, top_k_products=1)
    print(f" {time.perf_counter() - t0:.1f}s")


def _print_meta(meta: dict) -> None:
    d = meta["decision"]
    print()
    print(f"[router] tool={d['tool']}  conf={d['confidence']}  "
          f"rewritten={d['rewritten_query']!r}")
    if d.get("reasoning"):
        print(f"[router] reason: {d['reasoning']}")


def _print_tool_result(tr: dict) -> None:
    payload = tr.get("payload") or {}
    # New payload format: compact cards in products and full records in debug.hits_full.
    # Prefer full records here so CLI diagnostics include score and subcategory fields.
    hits = (payload.get("debug") or {}).get("hits_full") or payload.get("products") or []
    if hits:
        print(f"[tool={tr['tool_name']}] {len(hits)} hits:")
        for i, h in enumerate(hits, 1):
            score = h.get("score")
            score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "-"
            print(f"  {i}. {h.get('title')}  "
                  f"[{h.get('brand')} / {h.get('sub_category')}]  "
                  f"￥{h.get('base_price') or h.get('price')}  score={score_str}")
    else:
        payload_preview = json.dumps(payload, ensure_ascii=False)[:200]
        print(f"[tool={tr['tool_name']}] payload: {payload_preview}")
    print("\nAssistant: ", end="", flush=True)


def _run_streaming(agent: Agent, session: AgentSession, line: str) -> None:
    first_token_t: float | None = None
    t_start = time.perf_counter()
    final_event: dict | None = None
    try:
        for ev in agent.handle_turn_stream(line, session):
            t = ev["type"]
            if t == "meta":
                _print_meta(ev["data"])
            elif t == "tool_result":
                _print_tool_result(ev["data"])
            elif t == "token":
                if first_token_t is None:
                    first_token_t = time.perf_counter()
                print(ev["data"], end="", flush=True)
            elif t == "done":
                final_event = ev["data"]
            elif t == "error":
                print(f"\n[error] {ev['data']}", flush=True)
    except Exception as exc:
        print(f"\n[fatal] {exc!r}")
        return

    print()  # Finish the streamed line.
    total = time.perf_counter() - t_start
    ttfb = first_token_t - t_start if first_token_t else None
    timings = (final_event or {}).get("timings") or {}
    print(
        f"[timings] router={timings.get('router_ms','-')}ms  "
        f"tool={timings.get('tool_ms','-')}ms  "
        f"composer={timings.get('composer_ms','-')}ms  "
        f"first_token={int(ttfb*1000) if ttfb is not None else '-'}ms  "
        f"total={int(total*1000)}ms"
    )
    print()


def _run_non_streaming(agent: Agent, session: AgentSession, line: str) -> None:
    try:
        resp = agent.handle_turn(line, session)
    except Exception as exc:
        print(f"[fatal] {exc!r}")
        return
    _print_meta({"decision": resp.decision.to_dict()})
    _print_tool_result(resp.tool_result.to_dict())
    print(resp.narrative)
    print(f"\n[timings] {resp.trace.get('timings')}\n")


def main() -> int:
    log_level = os.getenv("LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "chromadb", "sentence_transformers", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    streaming = os.getenv("AGENT_STREAM", "1") != "0"

    print("Starting the agent. The first run loads models and vector indexes...")
    t_start = time.perf_counter()
    _warmup()
    print(f"Warmup complete in {time.perf_counter() - t_start:.1f}s\n")

    agent = Agent()
    session = AgentSession()
    mode = "stream" if streaming else "blocking"
    print(f"Session ID: {session.session_id}  mode: {mode}  (/exit to quit, /reset for a new session)")
    print("Ask a shopping question, for example: 500 以内的防晒霜\n")

    runner = _run_streaming if streaming else _run_non_streaming

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/reset":
            session = AgentSession()
            print(f"Reset complete. New session ID: {session.session_id}")
            continue

        runner(agent, session, line)


if __name__ == "__main__":
    sys.exit(main())
