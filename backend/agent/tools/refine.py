from __future__ import annotations

"""RefineTool：基于上一轮检索结果做增量精炼（"再便宜点"/"换国货"/"再来几个"）。

设计意图：
    用户多轮对话中 60% 是 refine——希望基于刚才那批商品做调整，
    而不是完全重新检索。如果硬塞回 RecommendTool 会：
        - 多调一次 llm_parser（慢）
        - 可能返回完全不同的一批商品（连贯性差）
        - "再便宜点" 没锚——不知道"多便宜"

    RefineTool 从 session.last_hits + session.last_parsed_query 出发，
    只在原 ParsedQuery 基础上做"增量调整"，并复用上一轮的召回池。

触发条件（由 router 第二段状态机判断）：
    - session 有 last_hits
    - query 命中 REFINE_PHRASES（"再便宜"/"换"/"还有别的"等）

TODO（待实现）：
    1. 从 query 抽取调整方向（价格上下调 / 品牌切换 / 排除某品类）
       - 简单规则 + LLM 兜底（function calling 抽 delta）
    2. 在 session.last_parsed_query 基础上叠加 delta，得到新的 ParsedQuery
    3. 复用上一轮召回池（session.get("last_chunks")），仅做后置过滤 + 重排
       - 如果池子被过滤空了，降级到重新走 SearchService.search()
    4. 返回 ToolResult 时 payload 标注 `refined_from: <last_query>`，
       composer 才能说"基于刚才那批我再帮你筛了..."
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult


class RefineTool:
    name: str = "refine"

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        # TODO: 真正实现 — 暂时返回占位，避免 router 路由到这里时整条管线崩溃
        last_hits = session.get("last_hits") or []
        return ToolResult(
            tool_name=self.name,
            payload={
                "query": query,
                "refined_from": session.get("last_parsed_query"),
                "previous_hits_count": len(last_hits),
                "products": [],  # 真实实现后填充
            },
            narrative_override=(
                "「再筛一筛」功能正在打磨中～你可以先换个具体说法，"
                "比如「300 元以内的同类」或「换个非苹果的品牌」。"
            ),
            needs_composer=False,
        )
