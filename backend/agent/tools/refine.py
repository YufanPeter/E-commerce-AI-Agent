from __future__ import annotations

"""RefineTool：基于上一轮检索意图做增量精炼（"再便宜点"/"换个品牌"/"Adidas"）。

设计意图：
    多轮对话里大量是 refine——用户只补一个约束（品牌/价格/颜色/软偏好），
    期望在"刚才那批"的基础上调整，而不是从零重检索。如果直接把这句短输入
    丢给 RecommendTool 全新检索，会丢掉上一轮的品类语义：
        "推荐跑鞋" → "Adidas"  ⇒  泛搜 Adidas（鞋/衣服/包都来了）

实现方式（轻量 State，无新依赖）：
    1. 从 working_memory 取上一轮**结构化**意图 last_parsed_query（ParsedQuery dict）。
    2. 还原成 ParsedQuery 作为 base，注入 slots["base_parsed"]。
    3. 复用 RecommendTool 流水线；SearchService 会用 ParsedQuery.merge_base 把
       本轮解析叠加到 base 上（品类/价格继承，品牌替换，否定/软偏好累积）。
    => "推荐跑鞋" → "Adidas" 被理解为 "Adidas 跑鞋"。

降级：没有上一轮结构化记忆（全新会话首轮、或旧版本存的是字符串）时，
      退回普通 recommend，不强行细化。
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.recommend import RecommendTool
from search.query_understanding import ParsedQuery


class RefineTool:
    name: str = "refine"

    def __init__(self, recommend: RecommendTool | None = None) -> None:
        # 复用 recommend 流水线；允许测试注入带 stub SearchService 的实例。
        self._recommend = recommend or RecommendTool()

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        base = self._rebuild_base(session.recall_parsed())
        last_hits = session.recall_hits()

        # 无上一轮结构化意图：退回普通推荐（首轮误判 / 旧字符串记忆）。
        if base is None:
            return self._recommend.run(query, session, dict(slots or {}))

        merged_slots = {**(slots or {}), "base_parsed": base}
        result = self._recommend.run(query, session, merged_slots)

        # 标注细化来源，composer 可据此说"在刚才那批里我又帮你筛了…"。
        # payload 是普通 dict，对 frozen 的 ToolResult 做就地写入是安全的。
        result.payload["refined_from"] = base.to_dict()
        result.payload["previous_hits_count"] = len(last_hits)
        return result

    @staticmethod
    def _rebuild_base(last_parsed: Any) -> ParsedQuery | None:
        """把 working_memory 里的 dict 还原成 ParsedQuery；非 dict 一律视为无 base。"""
        if isinstance(last_parsed, dict):
            try:
                return ParsedQuery.from_dict(last_parsed)
            except TypeError:
                return None
        return None
