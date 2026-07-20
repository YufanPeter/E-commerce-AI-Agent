from __future__ import annotations

"""Incrementally refine the previous retrieval intent.

Many follow-ups add only one constraint, such as brand, price, color, or a soft
preference. They should adjust the previous result set rather than start a new search.
A fresh recommendation for a short input would lose the prior category semantics:
        "推荐跑鞋" → "Adidas"  ⇒  泛搜 Adidas（鞋/衣服/包都来了）

Implementation:
    1. Read the structured ``last_parsed_query`` from working memory.
    2. Rebuild it as the base ``ParsedQuery`` in ``slots["base_parsed"]``.
    3. Reuse ``RecommendTool``; ``ParsedQuery.merge_base`` inherits category and price,
       replaces brand, and accumulates negative or soft preferences.

Without structured prior memory, fall back to an ordinary recommendation.
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.recommend import RecommendTool
from search.query_understanding import ParsedQuery


class RefineTool:
    name: str = "refine"

    def __init__(self, recommend: RecommendTool | None = None) -> None:
        # Reuse the recommendation pipeline and allow a stub service in tests.
        self._recommend = recommend or RecommendTool()

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        base = self._rebuild_base(session.recall_parsed())
        last_hits = session.recall_hits()

        # No structured prior intent: fall back to a regular recommendation.
        if base is None:
            return self._recommend.run(query, session, dict(slots or {}))

        merged_slots = {**(slots or {}), "base_parsed": base}
        result = self._recommend.run(query, session, merged_slots)

        # Record the refinement source for composer context. The payload is a mutable
        # dictionary, so updating it remains valid inside the frozen ToolResult.
        result.payload["refined_from"] = base.to_dict()
        result.payload["previous_hits_count"] = len(last_hits)
        return result

    @staticmethod
    def _rebuild_base(last_parsed: Any) -> ParsedQuery | None:
        """Rebuild ``ParsedQuery`` from working memory, ignoring non-dictionaries."""
        if isinstance(last_parsed, dict):
            try:
                return ParsedQuery.from_dict(last_parsed)
            except TypeError:
                return None
        return None
