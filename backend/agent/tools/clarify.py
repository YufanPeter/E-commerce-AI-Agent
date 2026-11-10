from __future__ import annotations

"""ClarifyTool：用户 query 太模糊时，引导用户补充信息。

Phase 1 用模板话术，不调 LLM：响应快、零成本、可控。
后续 Phase 可换成 LLM 生成更自然的澄清问句（结合品类/历史偏好）。
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from i18n import t


class ClarifyTool:
    name: str = "clarify"

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"query": query, "reason": "query too vague"},
            narrative_override=t("clarify.prompt"),
            needs_composer=False,
        )
