from __future__ import annotations

"""Orchestrator：把 router → tool → composer 串起来的总调度。

提供两个对外入口：
    - handle_turn():        非流式，一次性返回 AgentResponse
    - handle_turn_stream(): 生成器，按顺序 yield 结构化事件
                            （meta → tool_result → token* → done | error）

每个 turn 的流水线：

    user_query
        │
        ▼  ① intent_router.route()
    IntentDecision(tool, rewritten_query, confidence)
        │
        ▼  ② tool.run(rewritten_query, session, slots)
    ToolResult(payload, narrative_override?, composer_hint?)
        │
        ▼  ③ composer.compose() / compose_stream()  # 仅当 needs_composer=True
    narrative: str
        │
        ▼
    AgentResponse(decision, tool_result, narrative, trace)

降级策略：
    - router 抛异常 → 默认走 recommend（保守假设用户在购物）
    - tool 抛异常 → 走 fallback 话术 + 在 trace 记录错误
    - composer 抛异常（非流式）→ 用本地兜底文案（recommend 时给固定话术）
    - composer 流式中途异常 → composer 内部自己 yield 一段提示，
      orchestrator 仍把已 yield 的片段拼起来记入 session 历史
"""

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from agent.composer import AnswerComposer
from agent.intent_router import IntentDecision, KNOWN_TOOLS, route
from agent.session import AgentSession
from agent.tools.base import Tool, ToolResult
from agent.tools.clarify import ClarifyTool
from agent.tools.compare import CompareTool
from agent.tools.fallback import FallbackTool
from agent.tools.product_detail import ProductDetailTool
from agent.tools.recommend import RecommendTool
from agent.tools.refine import RefineTool


logger = logging.getLogger(__name__)


@dataclass
class AgentResponse:
    """一次 turn 的完整对外响应。"""

    decision: IntentDecision
    tool_result: ToolResult
    narrative: str
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "tool_result": self.tool_result.to_dict(),
            "narrative": self.narrative,
            "trace": self.trace,
        }


# ---------------------------------------------------------------------------
# 流式事件契约
# ---------------------------------------------------------------------------

# 流式 yield 出的 dict 用以下 type：
#   status:      阶段进度提示（人类可读，前端显示"识别中…"这类胶囊）
#   meta:        路由决策 + trace 元信息（结构化，给开发者/调试）
#   tool_result: 工具产出的结构化 payload（前端立刻渲染卡片）
#   token:       一段文本片段（拼接即为 narrative）
#   done:        本轮结束，附最终 timings
#   error:       致命错误（router / tool 阶段不可恢复时）
#
# 这是后端内部协议；FastAPI 适配层负责把它序列化为 SSE 行。

_DEFAULT_ROUTER_ERROR_TEXT = "（路由器调用失败，已退回到默认推荐流程）"

# 各工具对应的"正在工作中"提示文案
_TOOL_WORKING_HINT = {
    "recommend": "正在为你检索商品…",
    "refine": "正在重新筛选…",
    "compare": "正在对比商品…",
    "product_detail": "正在查看商品详情…",
    "clarify": "正在整理追问…",
    "fallback": "正在生成回复…",
}

# 各工具对应的"生成最终回复中"提示文案
_TOOL_COMPOSE_HINT = {
    "recommend": "正在生成推荐解说…",
    "refine": "正在生成筛选说明…",
    "compare": "正在生成对比总结…",
    "product_detail": "正在生成详细介绍…",
    "clarify": "",  # narrative_override，不会进 composer
    "fallback": "",
}


class Agent:
    """对外的总入口。FastAPI 路由层只持有一个 Agent 实例。"""

    def __init__(
        self,
        tools: dict[str, Tool] | None = None,
        composer: AnswerComposer | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = tools or self._default_tools()
        self._composer = composer or AnswerComposer()
        # 校验 router 已注册的 tool 都有对应实现
        missing = [t for t in KNOWN_TOOLS if t not in self._tools]
        if missing:
            raise RuntimeError(f"Missing tool implementations for: {missing}")

    @staticmethod
    def _default_tools() -> dict[str, Tool]:
        return {
            "recommend": RecommendTool(),
            "refine": RefineTool(),
            "compare": CompareTool(),
            "product_detail": ProductDetailTool(),
            "clarify": ClarifyTool(),
            "fallback": FallbackTool(),
        }

    # ----------------------------- 非流式 -----------------------------

    def handle_turn(self, query: str, session: AgentSession) -> AgentResponse:
        trace: dict[str, Any] = {"timings": {}}
        query = (query or "").strip()

        # 空输入：直接 clarify，省去一次 router LLM 调用
        if not query:
            return self._build_empty_query_response(session, trace)

        session.add_user(query)

        decision = self._safe_route(query, session, trace)
        tool_result = self._safe_run_tool(decision, session, trace)

        t2 = time.perf_counter()
        try:
            narrative = self._composer.compose(tool_result, session)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Composer failed")
            trace["composer_error"] = repr(exc)
            narrative = self._fallback_narrative(tool_result)
        trace["timings"]["composer_ms"] = int((time.perf_counter() - t2) * 1000)
        logger.info("composer done in %dms", trace["timings"]["composer_ms"])

        session.add_assistant(narrative)

        return AgentResponse(
            decision=decision,
            tool_result=tool_result,
            narrative=narrative,
            trace=trace,
        )

    # ----------------------------- 流式 -----------------------------

    def handle_turn_stream(
        self,
        query: str,
        session: AgentSession,
    ) -> Iterator[dict[str, Any]]:
        """流式版：按顺序 yield meta/tool_result/token*/done。"""
        trace: dict[str, Any] = {"timings": {}}
        query = (query or "").strip()

        # 空输入：与非流式保持一致——直接 clarify 文本走 token 通道。
        if not query:
            resp = self._build_empty_query_response(session, trace)
            yield {"type": "meta", "data": {
                "decision": resp.decision.to_dict(),
                "trace": resp.trace,
            }}
            yield {"type": "tool_result", "data": resp.tool_result.to_dict()}
            yield {"type": "token", "data": resp.narrative}
            yield {"type": "done", "data": {"timings": trace["timings"], "narrative": resp.narrative}}
            return

        session.add_user(query)

        # ① 路由
        yield {"type": "status", "data": {"phase": "routing", "message": "识别意图中…"}}
        decision = self._safe_route(query, session, trace)
        yield {"type": "meta", "data": {
            "decision": decision.to_dict(),
            "trace": {k: v for k, v in trace.items() if k != "timings"} | {"timings": dict(trace["timings"])},
        }}

        # ② 工具
        yield {"type": "status", "data": {
            "phase": "tool",
            "tool": decision.tool,
            "message": _TOOL_WORKING_HINT.get(decision.tool, "处理中…"),
        }}
        tool_result = self._safe_run_tool(decision, session, trace)
        yield {"type": "tool_result", "data": tool_result.to_dict()}

        # ③ Composer：流式
        if tool_result.needs_composer:
            compose_hint = _TOOL_COMPOSE_HINT.get(decision.tool) or "正在生成回复…"
            yield {"type": "status", "data": {"phase": "compose", "message": compose_hint}}
        t2 = time.perf_counter()
        narrative_parts: list[str] = []
        try:
            for piece in self._composer.compose_stream(tool_result, session):
                narrative_parts.append(piece)
                yield {"type": "token", "data": piece}
        except Exception as exc:  # noqa: BLE001 - 兜底，理论 composer_stream 已经吞了
            logger.exception("Streaming composer raised unexpectedly")
            trace["composer_error"] = repr(exc)
            fallback = self._fallback_narrative(tool_result)
            if not narrative_parts:
                narrative_parts.append(fallback)
                yield {"type": "token", "data": fallback}
        trace["timings"]["composer_ms"] = int((time.perf_counter() - t2) * 1000)

        narrative = "".join(narrative_parts).strip()
        if not narrative:
            # 极端情况下流没产出任何 token，给个兜底
            narrative = self._fallback_narrative(tool_result)
            yield {"type": "token", "data": narrative}

        session.add_assistant(narrative)

        yield {"type": "done", "data": {
            "timings": trace["timings"],
            "narrative": narrative,
            "trace": trace,
        }}

    # ----------------------------- 内部 -----------------------------

    def _safe_route(
        self, query: str, session: AgentSession, trace: dict[str, Any],
    ) -> IntentDecision:
        logger.info("turn start: query=%r", query[:60])
        t0 = time.perf_counter()
        try:
            decision = route(query, session)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Router failed, defaulting to recommend")
            trace["router_error"] = repr(exc)
            decision = IntentDecision(
                tool="recommend",
                rewritten_query=query,
                confidence="low",
                reasoning="router failure, default to recommend",
            )
        trace["timings"]["router_ms"] = int((time.perf_counter() - t0) * 1000)
        trace["decision"] = decision.to_dict()
        logger.info(
            "router done in %dms → tool=%s",
            trace["timings"]["router_ms"], decision.tool,
        )
        return decision

    def _safe_run_tool(
        self,
        decision: IntentDecision,
        session: AgentSession,
        trace: dict[str, Any],
    ) -> ToolResult:
        tool = self._tools[decision.tool]
        t1 = time.perf_counter()
        try:
            tool_result = tool.run(decision.rewritten_query, session, slots={})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %s failed", decision.tool)
            trace["tool_error"] = repr(exc)
            tool_result = ToolResult(
                tool_name=decision.tool,
                payload={"query": decision.rewritten_query, "error": repr(exc)},
                narrative_override="抱歉，系统暂时无法处理这个请求，请稍后再试或换种说法。",
                needs_composer=False,
            )
        trace["timings"]["tool_ms"] = int((time.perf_counter() - t1) * 1000)
        logger.info("tool %s done in %dms", decision.tool, trace["timings"]["tool_ms"])
        return tool_result

    def _build_empty_query_response(
        self, session: AgentSession, trace: dict[str, Any],
    ) -> AgentResponse:
        decision = IntentDecision(
            tool="clarify",
            rewritten_query="",
            confidence="high",
            reasoning="empty input shortcut",
        )
        tool_result = self._tools["clarify"].run("", session, slots={})
        return AgentResponse(
            decision=decision,
            tool_result=tool_result,
            narrative=tool_result.narrative_override or "",
            trace=trace,
        )

    @staticmethod
    def _fallback_narrative(tool_result: ToolResult) -> str:
        # composer 挂了时的兜底文案
        if tool_result.tool_name == "recommend":
            hits = tool_result.payload.get("products") or tool_result.payload.get("hits") or []
            if not hits:
                return "暂时没找到匹配的商品，可以放宽预算或换个关键词再试试～"
            titles = "、".join(h.get("title", "") for h in hits[:3])
            return f"为你找到 {len(hits)} 款商品，包括：{titles}。"
        return tool_result.narrative_override or "好的。"
