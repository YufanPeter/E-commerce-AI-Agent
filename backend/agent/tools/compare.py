from __future__ import annotations

"""CompareTool：对比上一轮命中的 2-3 个商品。

触发场景：
    - "这两个有什么区别"
    - "A 和 B 哪个适合干皮"
    - "对比一下第一个和第三个"
    - 主动型："帮我对比下"（默认取 last_hits 前 2 个）

设计意图：
    单纯把两个商品的全部 chunk 喂给 composer 会让 LLM 自由发挥——
    输出格式不可控、维度不一致、难以渲染成对比表格。
    所以这里把对比拆成两步：
        ① 结构化维度抽取（LLM function calling，限定 4-6 个维度）
        ② composer 仅给"什么人选哪个"的 1-2 句建议
    iOS 端拿到的是干净的 dimensions[] 表格 + summary 文本。

依赖：
    - ProductService.get_many(product_ids) → 取每个商品的全貌（spec/faq/review）
    - 上一轮的 session.last_hits（按用户说的"第一个"等定位）

TODO（待实现）：
    1. _extract_indices(query, last_hits) → list[int]
       支持："第一个"/"第1个"/"1和3"/"前两个"/"这俩"
       无明确指代时默认 [0, 1]
    2. _common_dimensions(products) → 结构化字段（价格/品牌/品类）
    3. _llm_extract_dynamic_dimensions(products) → 差异化维度
       prompt: "请挑 4 个最有差异性的维度，每个维度给两边的简短文本"
       用 function calling 限定返回 schema
    4. composer 适配：若 tool_name=='compare'，按"给建议"模式说话
"""

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult


class CompareTool:
    name: str = "compare"

    def __init__(self, product_service: Any | None = None) -> None:
        # TODO: 类型改成 ProductService（Step 1 后落地）
        self._product_service = product_service

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        # TODO: 真正实现
        last_hits = session.get("last_hits") or []
        if len(last_hits) < 2:
            return ToolResult(
                tool_name=self.name,
                payload={"query": query, "products": [], "dimensions": []},
                narrative_override=(
                    "还没有可对比的商品哦，先让我帮你推荐几款，"
                    "然后说「对比一下第一个和第二个」就可以。"
                ),
                needs_composer=False,
            )
        return ToolResult(
            tool_name=self.name,
            payload={
                "query": query,
                "products": last_hits[:2],
                "dimensions": [],  # 真实实现后填充
            },
            narrative_override="商品对比功能即将上线～敬请期待。",
            needs_composer=False,
        )
