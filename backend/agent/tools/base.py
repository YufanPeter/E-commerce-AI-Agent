from __future__ import annotations

"""Tool protocol and shared result contract.

Why Tool rather than Skill:
    Each current implementation is one atomic capability call, such as search or a
    static response. A future module that orchestrates several tools and decisions can
    introduce a separate Skill layer above this protocol.

Why Protocol rather than ABC:
    Implementations span different subsystems: recommendation wraps SearchService,
    clarification uses static templates, and fallback performs no I/O. A protocol
    constrains the interface without imposing inheritance.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent.session import AgentSession


@dataclass(frozen=True)
class ToolResult:
    """Shared return contract for every tool.

    ``payload`` contains JSON-serializable data rendered by the client.
    ``composer_hint`` adds private guidance for ``AnswerComposer``.
    ``narrative_override`` supplies a final response without an LLM call, which suits
    fixed clarification and fallback messages. When ``needs_composer`` is false, the
    orchestrator skips composition.
    """

    tool_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    composer_hint: str | None = None
    narrative_override: str | None = None
    needs_composer: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "payload": self.payload,
            "composer_hint": self.composer_hint,
            "narrative_override": self.narrative_override,
            "needs_composer": self.needs_composer,
        }


@runtime_checkable
class Tool(Protocol):
    """Interface contract implemented by all tools."""

    name: str

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        """Execute the tool.

        ``query`` is the original or router-rewritten user text. ``session`` contains
        conversation history, preferences, and working memory. ``slots`` contains
        additional router arguments such as ``rewritten_query`` and ``intent_args``.
        """
        ...
