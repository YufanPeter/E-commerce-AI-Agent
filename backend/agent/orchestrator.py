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
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from agent.composer import AnswerComposer
from agent.intent_router import IntentDecision, KNOWN_TOOLS, route
from agent.session import AgentSession
from agent.tools.base import Tool, ToolResult
from agent.tools.cart import CartTool
from agent.tools.clarify import ClarifyTool
from agent.tools.compare import CompareTool, is_compare_selection_reply
from agent.tools.fallback import FallbackTool
from agent.tools.product_detail import ProductDetailTool, is_detail_selection_reply
from agent.tools.reference import resolve_by_name, resolve_indices
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

_DETAIL_ATTRIBUTE_WORDS = (
    "续航", "性能", "屏幕", "拍照", "降噪", "配置", "重量", "轻薄",
    "评价", "口碑", "评论", "差评", "缺点", "问题",
    "敏感肌", "刺激", "酒精", "过敏", "泛红",
    "尺码", "偏大", "偏小", "合脚", "版型", "脚感",
)
_DETAIL_QUESTION_WORDS = (
    "怎么样", "如何", "好吗", "好不好", "行不行", "能不能", "适合吗",
    "有没有", "详细", "说说", "讲讲", "介绍", "表现",
)
_DETAIL_DEICTIC_WORDS = (
    "这款", "这个", "这件", "这双", "那款", "那个", "刚才", "刚刚", "上面", "前面",
)
_GROUP_COMPARE_WORDS = (
    "这些", "这几款", "这几个", "它们", "他们", "哪款", "哪个", "谁更", "哪个更", "哪款更",
)
_NEW_SEARCH_WORDS = (
    "推荐", "找", "看看", "看一下", "有哪些", "有什么", "想买", "给我", "来几款",
)
_BRAND_OR_MODEL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")

# 各工具对应的"正在工作中"提示文案
_TOOL_WORKING_HINT = {
    "recommend": "正在为你检索商品…",
    "refine": "正在重新筛选…",
    "compare": "正在对比商品…",
    "product_detail": "正在查看商品详情…",
    "cart": "正在处理购物车…",
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
            "cart": CartTool(),
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
        tool_result = self._safe_run_tool(decision, session, trace, raw_query=query)

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
        tool_result = self._safe_run_tool(decision, session, trace, raw_query=query)
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

    # ----------------------------- 图搜（拍照找货）-----------------------------

    def handle_image_turn_stream(
        self,
        image: str,
        session: AgentSession,
        hint_text: str = "",
    ) -> Iterator[dict[str, Any]]:
        """图片输入的流式入口：看图 → 抽检索词 → 视觉重排检索 → composer 流式。

        模态已知（就是图搜），无需 router LLM 分类，直接走 recommend 的图搜路径。
        ``hint_text`` 是用户随图附带的文字（图+文场景，可空）。
        """
        from llm.vision import UNRECOGNIZED, vision_extract_query

        trace: dict[str, Any] = {"timings": {}}
        image = (image or "").strip()
        hint_text = (hint_text or "").strip()

        # ① 看图：把图片抽成中文检索词
        yield {"type": "status", "data": {"phase": "vision", "message": "正在识别图片…"}}
        t0 = time.perf_counter()
        try:
            extracted = vision_extract_query(image)
        except Exception as exc:  # noqa: BLE001 - 视觉抽取失败要降级而非崩流
            logger.exception("vision_extract_query failed")
            trace["vision_error"] = repr(exc)
            extracted = UNRECOGNIZED
        trace["timings"]["vision_ms"] = int((time.perf_counter() - t0) * 1000)

        # 把这一轮用户输入记进历史（含可选附带文字），供 composer 上下文使用
        display = f"[图片] {hint_text}".strip() if hint_text else "[图片搜索]"
        session.add_user(display)

        # 图+文：把附带文字并入检索词，让"找便宜点的同款"这类约束也生效
        effective_query = extracted
        if extracted != UNRECOGNIZED and hint_text:
            effective_query = f"{extracted} {hint_text}"

        decision = IntentDecision(
            tool="recommend",
            rewritten_query=effective_query,
            confidence="high",
            reasoning="image visual search",
        )
        yield {"type": "meta", "data": {
            "decision": decision.to_dict(),
            "trace": {"timings": dict(trace["timings"]), "extracted_query": extracted},
        }}

        # 识别失败：不检索，友好澄清
        if extracted == UNRECOGNIZED:
            text = "没看清这张图里的商品呢～换张更清晰的图，或者用文字描述一下你想找什么？"
            yield {"type": "tool_result", "data": {
                "tool_name": "recommend",
                "payload": {"action": "image_unrecognized", "products": []},
            }}
            yield {"type": "token", "data": text}
            session.add_assistant(text)
            yield {"type": "done", "data": {"timings": trace["timings"], "narrative": text}}
            return

        # ② 工具：图搜 + 视觉重排
        yield {"type": "status", "data": {
            "phase": "tool", "tool": "recommend", "message": "正在按图找相似商品…",
        }}
        t1 = time.perf_counter()
        recommend = self._tools["recommend"]
        try:
            tool_result = recommend.run_image(effective_query, image, session)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Visual search tool failed")
            trace["tool_error"] = repr(exc)
            tool_result = ToolResult(
                tool_name="recommend",
                payload={"query": effective_query, "products": [], "error": repr(exc)},
                narrative_override="抱歉，图片搜索暂时不可用，换种说法或稍后再试？",
                needs_composer=False,
            )
        trace["timings"]["tool_ms"] = int((time.perf_counter() - t1) * 1000)
        yield {"type": "tool_result", "data": tool_result.to_dict()}

        # ③ composer 流式（与文本链路一致）
        if tool_result.needs_composer:
            yield {"type": "status", "data": {"phase": "compose", "message": "正在生成推荐解说…"}}
        t2 = time.perf_counter()
        narrative_parts: list[str] = []
        try:
            for piece in self._composer.compose_stream(tool_result, session):
                narrative_parts.append(piece)
                yield {"type": "token", "data": piece}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Streaming composer raised unexpectedly (image path)")
            trace["composer_error"] = repr(exc)
            fallback = self._fallback_narrative(tool_result)
            if not narrative_parts:
                narrative_parts.append(fallback)
                yield {"type": "token", "data": fallback}
        trace["timings"]["composer_ms"] = int((time.perf_counter() - t2) * 1000)

        narrative = "".join(narrative_parts).strip()
        if not narrative:
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
        # 若上一轮 cart 正在等用户选规格或选商品，则本轮强制走 cart，跳过 router LLM——
        # 否则"暗夜黑 42码""第一个"这类回答会被路由误判为 refine/recommend。
        # 例外：用户明确改口开新检索（“推荐几款笔记本”“有没有便宜点的平板”），这类话
        # 带检索动词、不会是规格应答，应释放 pending 交回正常路由，而不是硬塞进 cart。
        if session.get("pending_cart") or session.get("pending_add"):
            if _is_new_search_escape(query):
                session.set("pending_cart", None)
                session.set("pending_add", None)
                logger.info("pending cart released: new-search escape detected")
            else:
                decision = IntentDecision(
                    tool="cart",
                    rewritten_query=query,
                    confidence="high",
                    reasoning="pending cart selection",
                )
                trace["timings"]["router_ms"] = 0
                trace["decision"] = decision.to_dict()
                logger.info("router skipped: pending cart selection → tool=cart")
                return decision
        # 若上一轮 product_detail 正在追问“这几款里要哪一款”，且本轮像一次选择应答
        # （“第一款”/品牌名/指代），强制走 product_detail，避免“第一款”被 router
        # 误判成 recommend/refine 而把序号映射到无关商品。换了话题则释放待定态。
        if session.get("pending_detail"):
            if is_detail_selection_reply(query):
                decision = IntentDecision(
                    tool="product_detail",
                    rewritten_query=query,
                    confidence="high",
                    reasoning="pending detail clarification",
                )
                trace["timings"]["router_ms"] = 0
                trace["decision"] = decision.to_dict()
                logger.info("router skipped: pending detail clarification → tool=product_detail")
                return decision
            session.set("pending_detail", None)
        # 同理：上一轮 compare 追问“想对比哪几款”，本轮像“第一个和第三个”
        # 这样的选择应答 → 强制走 compare，避免被误判成 product_detail/refine。
        if session.get("pending_compare"):
            hit_count = len(session.recall_hits())
            if is_compare_selection_reply(query, hit_count):
                decision = IntentDecision(
                    tool="compare",
                    rewritten_query=query,
                    confidence="high",
                    reasoning="pending compare clarification",
                )
                trace["timings"]["router_ms"] = 0
                trace["decision"] = decision.to_dict()
                logger.info("router skipped: pending compare clarification → tool=compare")
                return decision
            session.set("pending_compare", None)
        t0 = time.perf_counter()
        try:
            decision = route(query, session)
        except Exception as exc:  # noqa: BLE001
            # router LLM 偶发抽风（返回空响应/不调用 route_to_tool）。粗暴默认
            # recommend 会把"把小米加入购物车"误当搜索。这里按关键词做确定性
            # 兜底：明显是管车/下单意图时兜到 cart，否则才退回 recommend。
            fallback_tool = self._keyword_fallback_tool(query)
            logger.warning(
                "Router failed (%r), keyword fallback → %s", exc, fallback_tool
            )
            trace["router_error"] = repr(exc)
            decision = IntentDecision(
                tool=fallback_tool,
                rewritten_query=query,
                confidence="low",
                reasoning=f"router failure, keyword fallback to {fallback_tool}",
            )
        trace["timings"]["router_ms"] = int((time.perf_counter() - t0) * 1000)
        decision = self._apply_post_route_guards(query, session, decision)
        trace["decision"] = decision.to_dict()
        logger.info(
            "router done in %dms → tool=%s",
            trace["timings"]["router_ms"], decision.tool,
        )
        return decision

    @staticmethod
    def _apply_post_route_guards(
        query: str,
        session: AgentSession,
        decision: IntentDecision,
    ) -> IntentDecision:
        """对少数高确定性意图做路由后保护，压住 LLM 抖动。"""
        raw_text = query or ""
        compare_words = ("对比", "比较", "区别", "哪个好", "哪款好", "哪个更", "哪款更")
        if decision.tool == "recommend" and any(w in raw_text for w in compare_words):
            return IntentDecision(
                tool="compare",
                rewritten_query=decision.rewritten_query or query,
                confidence="high",
                reasoning=f"post-route guard: compare intent detected; original={decision.tool}",
            )
        if decision.tool in {"recommend", "refine"}:
            guarded = _guard_attribute_followup(query, session, decision)
            if guarded is not None:
                return guarded
        return decision

    @staticmethod
    def _keyword_fallback_tool(query: str) -> str:
        """router LLM 失败时的确定性兜底：按关键词粗判工具，避免一律 recommend。

        只覆盖最确定的"管车/下单"意图（加购、删除、改数量、查看车、结算）；
        其余情况仍退回 recommend（搜索/推荐是最安全的默认）。"""
        text = query or ""
        cart_words = (
            "加入购物车", "加购", "购物车", "加进来", "来一件", "来一个",
            "下单", "结算", "买这些", "就买", "删掉", "删除", "去掉",
            "不要了", "改成", "数量",
        )
        if any(w in text for w in cart_words):
            return "cart"
        return "recommend"

    def _safe_run_tool(
        self,
        decision: IntentDecision,
        session: AgentSession,
        trace: dict[str, Any],
        raw_query: str = "",
    ) -> ToolResult:
        tool = self._tools[decision.tool]
        t1 = time.perf_counter()
        try:
            # compare/product_detail 这类强依赖“第一个/最后一个/这款”的工具，
            # 不能让 router 的 rewritten_query 改写掉用户原始指代；否则 LLM 一旦
            # 把“第一个”误补成错误商品名，工具就会对比错对象。
            tool_query = raw_query if decision.tool in {"compare", "product_detail"} else decision.rewritten_query
            tool_result = tool.run(tool_query, session, slots={})
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


def _guard_attribute_followup(
    query: str,
    session: AgentSession,
    decision: IntentDecision,
) -> IntentDecision | None:
    """Route context-dependent attribute questions away from fresh recommendation.

    Examples:
    - ``小米的续航怎么样`` after a phone list -> product_detail (or clarification in tool)
    - ``这几款哪个续航更好`` -> compare
    - ``推荐续航好的平板`` -> keep recommend/refine
    """
    hits = session.recall_hits()
    if not hits:
        return None

    raw_text = query or ""
    text = f"{raw_text} {decision.rewritten_query or ''}"
    if not any(word in text for word in _DETAIL_ATTRIBUTE_WORDS):
        return None

    if any(word in raw_text for word in _GROUP_COMPARE_WORDS):
        return IntentDecision(
            tool="compare",
            rewritten_query=query,
            confidence="high",
            reasoning=f"post-route guard: group attribute follow-up; original={decision.tool}",
        )

    if _is_new_search_attribute_request(query or ""):
        return None

    has_question_signal = any(word in text for word in _DETAIL_QUESTION_WORDS)
    if has_question_signal and _has_detail_target_signal(query, hits):
        return IntentDecision(
            tool="product_detail",
            rewritten_query=query,
            confidence="high",
            reasoning=f"post-route guard: product attribute follow-up; original={decision.tool}",
        )
    focus_id = session.get("last_focus_product_id")
    if has_question_signal and focus_id and any(hit.get("product_id") == focus_id for hit in hits):
        return IntentDecision(
            tool="product_detail",
            rewritten_query=query,
            confidence="high",
            reasoning=f"post-route guard: focused product attribute follow-up; original={decision.tool}",
        )
    return None


def _is_new_search_attribute_request(text: str) -> bool:
    if not any(word in text for word in _NEW_SEARCH_WORDS):
        return False
    return not any(word in text for word in _DETAIL_DEICTIC_WORDS)


def _has_detail_target_signal(query: str, hits: list[dict[str, Any]]) -> bool:
    text = query or ""
    if any(word in text for word in _DETAIL_DEICTIC_WORDS):
        return True
    if len(resolve_indices(text, len(hits))) == 1:
        return True
    if resolve_by_name(text, hits):
        return True

    query_tokens = set(_BRAND_OR_MODEL_TOKEN_RE.findall(text))
    if not query_tokens:
        return False
    for hit in hits:
        title_tokens = set(_BRAND_OR_MODEL_TOKEN_RE.findall(str(hit.get("title", ""))))
        brand_tokens = set(_BRAND_OR_MODEL_TOKEN_RE.findall(str(hit.get("brand", ""))))
        if query_tokens & (title_tokens | brand_tokens):
            return True
    return False


# 明确的「开新检索/换品类」逃逸信号——用户在 pending（选规格/选商品）中途改口
# 想找别的东西。要求出现强检索动词，且不是纯指代应答；这类话绝不会是
# “暗夜黑/42码/第一个”这种 pending 应答，因此放宽逃逸不会误伤选规格/选商品流程。
_ESCAPE_SEARCH_WORDS: tuple[str, ...] = (
    "推荐", "有没有", "有什么", "想看", "想买", "看看别的", "换成", "换个", "再找", "找找",
    "其他", "别的",
)


def _is_new_search_escape(query: str) -> bool:
    """判断 pending 中途这句是否是“改口开新检索”，用于释放 cart 的 pending 态。

    命中条件：含强检索动词（推荐/有没有/换成…）且不含「这款/这个」等聚焦指代。
    纯指代应答（“这个”“第一个”）不算逃逸，继续留在 pending 让用户完成加购。"""
    text = (query or "").strip()
    if not text:
        return False
    if any(word in text for word in _DETAIL_DEICTIC_WORDS):
        return False
    return any(word in text for word in _ESCAPE_SEARCH_WORDS)

