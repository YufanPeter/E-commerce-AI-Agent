from __future__ import annotations

"""FallbackTool：处理与购物无关的 query，礼貌地把对话拉回正轨。"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from i18n import t


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
            narrative_override=t("fallback.prompt"),
            needs_composer=False,
        )

