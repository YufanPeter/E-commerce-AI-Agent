from __future__ import annotations

"""Session state combining long-term preferences, history, and working memory.

V1 memory layers:

    Preference Core Memory
        - One record per user ID, retained across sessions.
        - Loaded by the API into ``user_profile``; this class does not persist it.

    Working memory
        - A temporary dictionary shared by tools and the composer.
        - Typical fields include ``last_parsed_query`` and ``last_hits``.

    Session memory
        - Raw conversation messages retained in a sliding window.
        - Gives the router enough context to resolve follow-up references.
        - Older messages are compressed into a lightweight session-only summary.

Phase 1 assumes one FastAPI worker and serialized calls per session.
"""

from dataclasses import dataclass, field
from typing import Any, TypedDict
from uuid import uuid4


# ---------------------------------------------------------------------------
# Working-memory type contract
# ---------------------------------------------------------------------------
#
# ``working_memory`` was previously an untyped dictionary whose keys were documented
# only inside individual tools. A renamed key could silently break the chain. TypedDict
# provides one discoverable contract for names, types, and meanings.
#
# TypedDict is a type-level view only; runtime values remain ordinary dictionaries,
# preserving compatibility with set/get and adding no dependency.


class HitRef(TypedDict):
    """Compact product reference retained from the previous turn."""

    product_id: str
    title: str


class WorkingMemory(TypedDict, total=False):
    """Optional working-memory fields reused across turns.

    ``last_parsed_query`` is the previous structured retrieval intent and provides the
    base for lossless refinement. ``last_hits`` contains compact references used by
    compare and product-detail tools to resolve ordinal or demonstrative references.
    """

    last_parsed_query: dict[str, Any]
    last_hits: list[HitRef]
    session_summary: str
    summary_updated_at_turn: int


# Retain the latest N user/assistant messages to bound context-token growth.
DEFAULT_HISTORY_WINDOW = 10


@dataclass
class Message:
    """Conversation message using the standard chat role convention."""

    role: str          # "user" / "assistant" / "system"
    content: str


@dataclass
class AgentSession:
    """State for one user conversation."""

    session_id: str = field(default_factory=lambda: uuid4().hex)
    user_id: str | None = None
    user_profile: dict[str, Any] = field(default_factory=dict)
    history: list[Message] = field(default_factory=list)
    # Lightweight context accumulated across turns; see individual tool contracts.
    working_memory: dict[str, Any] = field(default_factory=dict)
    history_window: int = DEFAULT_HISTORY_WINDOW

    def add_user(self, content: str) -> None:
        self._append(Message(role="user", content=content))

    def add_assistant(self, content: str) -> None:
        self._append(Message(role="assistant", content=content))

    def _append(self, msg: Message) -> None:
        self.history.append(msg)
        # This class retains user/assistant messages only; system messages live elsewhere.
        if len(self.history) > self.history_window:
            overflow = self.history[: len(self.history) - self.history_window]
            self._update_session_summary(overflow)
            self.history = self.history[-self.history_window:]

    def recent_text(self, n: int = 4) -> str:
        """Return recent text for router and composer context."""
        recent = self.history[-n:]
        lines = []
        summary = self.working_memory.get("session_summary")
        if summary:
            lines.append(f"会话摘要: {summary}")
        for m in recent:
            prefix = "用户" if m.role == "user" else "助手"
            lines.append(f"{prefix}: {m.content}")
        return "\n".join(lines)

    def _update_session_summary(self, overflow: list[Message]) -> None:
        if not overflow:
            return
        existing = str(self.working_memory.get("session_summary") or "").strip()
        snippets = []
        for msg in overflow:
            prefix = "用户" if msg.role == "user" else "助手"
            content = " ".join((msg.content or "").split())
            if content:
                snippets.append(f"{prefix}:{content[:80]}")
        combined = "；".join([s for s in [existing, *snippets] if s])
        # Use a deterministic summary to avoid adding an LLM failure point to persistence.
        self.working_memory["session_summary"] = combined[-500:]
        self.working_memory["summary_updated_at_turn"] = (
            int(self.working_memory.get("summary_updated_at_turn") or 0) + len(overflow)
        )

    def set(self, key: str, value: Any) -> None:
        self.working_memory[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.working_memory.get(key, default)

    # Typed accessors around the WorkingMemory contract.

    def remember_search(self, parsed_dict: dict[str, Any], hits: list[HitRef]) -> None:
        """Store the successful search intent and hits for subsequent tools."""
        self.working_memory["last_parsed_query"] = parsed_dict
        self.working_memory["last_hits"] = hits

    def recall_parsed(self) -> dict[str, Any] | None:
        """Return the previous structured retrieval intent, if any."""
        return self.working_memory.get("last_parsed_query")

    def recall_hits(self) -> list[HitRef]:
        """Return product references from the previous turn."""
        return self.working_memory.get("last_hits") or []
