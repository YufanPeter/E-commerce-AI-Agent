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

import re
from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.recommend import RecommendTool
from search.query_understanding import ParsedQuery


_MORE_PHRASE_RE = re.compile(
    r"(更多|还有别的|还有其他|还有其它|还有吗|还有没有|再来几款|再来点|再来一些|换一批|"
    r"再看看|看看更多|看看别的|看看其他|看看其它)(的|款|商品)?(吗|呢|了)?"
)


def parse_more_request(query: str) -> str | None:
    """识别"看更多"：返回要看哪一组（纯粹要更多时是空串），不是这类请求返回 None。

    客户端的"查看更多"发的是"更多<分区名>"。这里只认整句都是在要更多的情况，
    "还有便宜点的吗"带了新约束，交给 router 走普通 refine。"""
    text = (query or "").strip().rstrip("？?。.!！~～ ")
    if _MORE_PHRASE_RE.fullmatch(text):
        return ""
    if text.startswith("更多") and len(text) > 2:
        return text[2:].strip()
    return None


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
