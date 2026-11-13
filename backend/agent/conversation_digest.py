"""One digest per conversation, so a later chat can recall it or pick it up again.

After every turn the facts are rebuilt from the conversation itself (what was searched,
the budget, products compared, added or ordered, and what was on screen). They are exact
and cost nothing. The title and one-sentence summary, which can carry what only the
shopper's words say ("a gift for my mom"), are then rewritten by the model in the
background; until that lands, a summary built from the facts stands in.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.memory_service import _llm_pool, _local_day, llm_extract_enabled
from agent.session import AgentSession
from i18n import en as english
from i18n import is_english
from store.user_memory_store import UserMemoryStore

logger = logging.getLogger(__name__)

_ACTIONS = {"view_detail": "viewed", "compare": "compared", "add_cart": "added", "checkout": "ordered"}
_PER_ACTION = 4
_SNAPSHOT = 5


def digest_facts(session: AgentSession, events: list[dict[str, Any]]) -> dict[str, Any]:
    """What the conversation was about, from its own state and events."""
    task = session.task
    constraints = task.constraints if isinstance(task.constraints, dict) else {}
    types = [e.get("sub_category") for e in events if e.get("type") == "search"] + [constraints.get("sub_category")]
    facts: dict[str, Any] = {
        "first_message": next((m.content for m in session.history if m.role == "user"), "")[:200],
        "types": [t for t in dict.fromkeys(types) if t],
        "max_price": constraints.get("max_price"),
        "min_price": constraints.get("min_price"),
        "brands": list(constraints.get("brand_include") or []),
    }
    for kind, label in _ACTIONS.items():
        titles = [str(e["title"]) for e in events if e.get("type") == kind and e.get("title")]
        facts[label] = list(dict.fromkeys(titles))[-_PER_ACTION:]
    items = task.shopping_items if task.active_view.kind == "cart" else task.active_view.items
    facts["products"] = [
        {"product_id": item["product_id"], "title": str(item.get("title") or "")[:120]}
        for item in items[:_SNAPSHOT] if item.get("product_id")
    ]
    facts["constraints"] = constraints
    return facts


def _short(title: str) -> str:
    return title.split(",")[0].split(" - ")[0][:60].strip()


def facts_summary(facts: dict[str, Any]) -> str:
    """A plain summary from the facts alone, used until the model's arrives."""
    parts = []
    if facts.get("types"):
        budget = f" under ${facts['max_price']:g}" if facts.get("max_price") else ""
        parts.append(f"looked for {', '.join(facts['types'][:3])}{budget}")
    for label in ("compared", "added", "ordered"):
        if facts.get(label):
            parts.append(f"{label} {', '.join(_short(t) for t in facts[label][:3])}")
    text = "; ".join(parts)
    return text[:1].upper() + text[1:] if text else ""


def _turns(session: AgentSession) -> int:
    return len(session.history) + int(session.working_memory.get("summary_updated_at_turn") or 0)


def update_digest(store: UserMemoryStore, session: AgentSession) -> None:
    """Refresh this conversation's digest after a turn. Never fails the turn."""
    user_id = session.user_id
    if not user_id or not session.session_id or (session.user_profile or {}).get("personalization_enabled") is False:
        return
    try:
        events = store.session_events(user_id, session.session_id)
        if not events:
            # Only a conversation that did something of its own is remembered: small talk is not,
            # and neither is one that only recalled an earlier conversation.
            return
        facts = digest_facts(session, events)
        turns = _turns(session)
        title = facts["first_message"][:60] or (facts["types"] or ["Shopping"])[0]
        store.save_digest_facts(user_id, session.session_id, title, facts_summary(facts), facts, turns)
    except Exception:  # noqa: BLE001 - memory must not break the conversation
        logger.exception("Updating the conversation digest failed")
        return
    if llm_extract_enabled():
        messages = [m.content for m in session.history if m.role == "user"][-12:]
        earlier = str(session.working_memory.get("session_summary") or "")
        _llm_pool.submit(_write_text, store, user_id, session.session_id, messages, earlier, facts, turns)


def _write_text(
    store: UserMemoryStore, user_id: str, session_id: str, messages: list[str], earlier: str,
    facts: dict[str, Any], turns: int,
) -> None:
    try:
        written = write_digest_text(messages, earlier, facts)
        if written:
            store.save_digest_text(user_id, session_id, written[0], written[1], turns)
    except Exception:  # noqa: BLE001
        logger.warning("Writing the conversation summary failed", exc_info=True)


def write_digest_text(messages: list[str], earlier: str, facts: dict[str, Any], timeout: float = 8.0) -> tuple[str, str] | None:
    """The model's title and summary, or ``None`` when it gives nothing usable."""
    from llm.client import get_client, get_model_id

    shown = {k: v for k, v in facts.items() if k not in {"constraints", "products", "first_message"} and v}
    content = "\n".join([
        *(["Earlier in the conversation: " + earlier[:500]] if earlier else []),
        "Shopper's messages:", *(f"- {m[:200]}" for m in messages),
        "What happened: " + json.dumps(shown, ensure_ascii=False),
    ])
    response = get_client().chat.completions.create(
        model=get_model_id(role="fast"),
        messages=[
            {"role": "system", "content": english.DIGEST_SYSTEM if is_english() else _ZH_SYSTEM},
            {"role": "user", "content": content},
        ],
        tools=[_TOOL],
        tool_choice={"type": "function", "function": {"name": "write_digest"}},
        temperature=0.0,
        timeout=timeout,
    )
    calls = response.choices[0].message.tool_calls
    if not calls:
        return None
    args = json.loads(calls[0].function.arguments or "{}")
    title, summary = " ".join(str(args.get("title") or "").split()), " ".join(str(args.get("summary") or "").split())
    return (title[:80], summary[:300]) if title and summary else None


_TOOL = {
    "type": "function",
    "function": {
        "name": "write_digest",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["title", "summary"],
        },
    },
}
_ZH_SYSTEM = (
    "为一段购物对话写记忆摘要，调用 write_digest。title：不超过 12 个字，说清在买什么（有对象或场合就带上）。"
    "summary：一两句话，写清在找什么、为谁或为什么（用户说过才写）、关键条件、最后的决定或加购。只写对话里出现过的内容。"
)


def conversation_trail(store: UserMemoryStore, user_id: str | None, exclude_session: str | None, limit: int = 6) -> list[dict[str, Any]]:
    """Earlier conversations, newest first, for the router to match."""
    if not user_id:
        return []
    try:
        rows = store.list_digests(user_id, limit=limit, exclude_session=exclude_session)
    except Exception:  # noqa: BLE001 - without the trail the question is answered as usual
        logger.warning("Reading conversation digests failed", exc_info=True)
        return []
    return [
        {
            "session_id": row["session_id"], "title": row["title"], "at": _local_day(row["updated_at"]),
            "summary": row["summary"] or facts_summary(row["facts"]), "facts": row["facts"],
        }
        for row in rows
    ]
