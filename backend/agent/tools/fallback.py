from __future__ import annotations

"""FallbackTool：处理与购物无关的 query，礼貌地把对话拉回正轨。"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult


_FALLBACK_TEXT = (
    "抱歉，我是电商导购助手，专门帮你挑选商品。"
    "你可以告诉我想买的品类、预算或使用场景，我会给出推荐～"
)


class FallbackTool:
    name: str = "fallback"

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"query": query, "reason": "out of scope"},
            narrative_override=_FALLBACK_TEXT,
            needs_composer=False,
        )

