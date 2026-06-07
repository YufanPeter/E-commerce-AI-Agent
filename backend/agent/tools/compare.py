from __future__ import annotations

"""CompareTool：对比上一轮命中的 2-3 个商品。

触发场景：
    - "这两个有什么区别"
    - "A 和 B 哪个适合干皮"
    - "对比一下第一个和第三个"
    - 主动型："帮我对比下"（默认取 last_hits 前 2 个）

设计意图：
    把对比拆成"定位 + 结构化抽取"两步：
        ① 用 reference 模块把"第一个和第三个"/"华为和小米"映射回具体商品；
        ② 用 agent.comparison.build_comparison 产出干净的维度表 + 购买建议。
    iOS 端拿到的是可直接渲染的 comparison 结构（products + rows + recommendation），
    而不是一段自由发挥的文本。

依赖：
    - ProductStore.get_product_detail：取每个商品的全貌（spec/faq/review）
    - session.recall_hits()：上一轮命中商品，供指代定位
"""

import logging
from typing import Any

from agent.comparison import build_comparison
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.reference import resolve_by_name, resolve_indices
from store.product_store import ProductStore


logger = logging.getLogger(__name__)

# 固定对比 2 个商品（对比表两列最清晰，也是用户预期）。
_MAX_COMPARE = 2


class CompareTool:
    name: str = "compare"

    def __init__(self, product_store: ProductStore | None = None) -> None:
        self._store = product_store or ProductStore()

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        last_hits = session.recall_hits()
        if len(last_hits) < 2:
            return self._plain(
                "还没有可对比的商品哦，先让我帮你推荐几款，"
                "然后说「对比一下第一个和第二个」就可以。"
            )

        # 前端对比页可能直接指定商品 id（绕过指代解析）。
        explicit_ids = slots.get("product_ids")
        if explicit_ids:
            product_ids = [pid for pid in explicit_ids][:_MAX_COMPARE]
        else:
            product_ids = self._resolve_targets(query, last_hits)

        if len(product_ids) < 2:
            return self._plain(
                "想对比哪几款呢？可以说「对比第一个和第三个」，"
                "或者「对比华为和小米那两款」。"
            )

        details = []
        for pid in product_ids:
            detail = self._store.get_product_detail(pid)
            if detail is not None:
                details.append(detail)
        if len(details) < 2:
            return self._plain("这些商品的资料不太全，换两款再试试对比？")

        focus = slots.get("focus") or query
        try:
            comparison = build_comparison(details, focus=focus)
        except Exception:  # noqa: BLE001 - 兜底，绝不让对比把整个 turn 打挂
            logger.exception("build_comparison failed")
            return self._plain("对比生成出了点问题，稍后再试或换两款商品？")

        # 把参与对比的商品回写工作记忆，便于后续"第一个加入购物车"等接续指代。
        session.set(
            "last_hits",
            [
                {"product_id": p["product_id"], "title": p["title"]}
                for p in comparison["products"]
            ],
        )

        # 对话里给一句中性引导（不再给选购建议），让用户看下面的对比表自行判断。
        titles = "」「".join(p["title"][:14] for p in comparison["products"])
        narrative = f"已为你对比「{titles}」，下面是各维度对照，可按自己看重的方面来选～"

        return ToolResult(
            tool_name=self.name,
            payload={"action": "compare", "comparison": comparison},
            narrative_override=narrative,
            needs_composer=False,
        )

    def _resolve_targets(
        self, query: str, last_hits: list[dict[str, Any]]
    ) -> list[str]:
        """把用户这句话定位到要对比的商品 id 列表（保序去重，最多 _MAX_COMPARE）。

        优先级：显式序号（"第一个和第三个"）→ 品牌/名称（"华为和小米"）→
        都没说时默认前两个。"""
        indices = resolve_indices(query, len(last_hits))
        if len(indices) < 2:
            named = resolve_by_name(query, last_hits)
            for i in named:  # 合并序号与名称命中，保序去重
                if i not in indices:
                    indices.append(i)
        if len(indices) < 2:
            indices = list(range(min(2, len(last_hits))))  # 没点名 → 默认前两个

        ordered: list[str] = []
        for i in indices[:_MAX_COMPARE]:
            pid = last_hits[i]["product_id"]
            if pid not in ordered:
                ordered.append(pid)
        return ordered

    def _plain(self, text: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"action": "compare", "comparison": None},
            narrative_override=text,
            needs_composer=False,
        )

