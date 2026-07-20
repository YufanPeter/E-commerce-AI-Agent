from __future__ import annotations

"""Top-level orchestration from router to tool to answer composer.

Public entry points:
    - ``handle_turn`` returns one complete ``AgentResponse``.
    - ``handle_turn_stream`` yields structured events in order:
      meta, tool_result, zero or more tokens, then done or error.

Turn pipeline:

    user_query
        │
        ▼  ① intent_router.route()
    IntentDecision(tool, rewritten_query, confidence)
        │
        ▼  ② tool.run(rewritten_query, session, slots)
    ToolResult(payload, narrative_override?, composer_hint?)
        │
        ▼  ③ composer.compose() / compose_stream()  # only when needs_composer=True
    narrative: str
        │
        ▼
    AgentResponse(decision, tool_result, narrative, trace)

Fallback strategy:
    - Router failures default to recommendation.
    - Tool failures produce a fallback response and record the error in the trace.
    - Blocking composer failures use a local fallback message.
    - Streaming composer failures append a fallback; emitted pieces are still stored in
      session history.
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
    """Complete public response for one turn."""

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
# Streaming event contract
# ---------------------------------------------------------------------------

# Streamed dictionaries use these event types:
#   status: human-readable phase progress for the client
#   meta: router decision and structured trace metadata
#   tool_result: structured payload that the client can render immediately
#   token: one text fragment; concatenation forms the narrative
#   done: end of turn with final timings
#   error: unrecoverable router or tool failure
#
# This is an internal protocol; the FastAPI adapter serializes it as SSE.

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

# User-facing in-progress messages by tool.
_TOOL_WORKING_HINT = {
    "recommend": "正在为你检索商品…",
    "refine": "正在重新筛选…",
    "compare": "正在对比商品…",
    "product_detail": "正在查看商品详情…",
    "cart": "正在处理购物车…",
    "clarify": "正在整理追问…",
    "fallback": "正在生成回复…",
}

# User-facing composition messages by tool.
_TOOL_COMPOSE_HINT = {
    "recommend": "正在生成推荐解说…",
    "refine": "正在生成筛选说明…",
    "compare": "正在生成对比总结…",
    "product_detail": "正在生成详细介绍…",
        "clarify": "",  # Uses narrative_override and skips the composer.
    "fallback": "",
}


class Agent:
    """Public orchestrator; the FastAPI layer owns one shared instance."""

    def __init__(
        self,
        tools: dict[str, Tool] | None = None,
        composer: AnswerComposer | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = tools or self._default_tools()
        self._composer = composer or AnswerComposer()
        # Verify that every router tool has an implementation.
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

    # ----------------------------- Blocking -----------------------------

    def handle_turn(self, query: str, session: AgentSession) -> AgentResponse:
        trace: dict[str, Any] = {"timings": {}}
        query = (query or "").strip()

        # Clarify empty input directly and avoid a router LLM call.
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

    # ----------------------------- Streaming -----------------------------

    def handle_turn_stream(
        self,
        query: str,
        session: AgentSession,
    ) -> Iterator[dict[str, Any]]:
        """Yield meta, tool result, text chunks, and completion in order."""
        trace: dict[str, Any] = {"timings": {}}
        query = (query or "").strip()

        # Match blocking behavior for empty input and emit clarification as a token.
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

        # Route.
        yield {"type": "status", "data": {"phase": "routing", "message": "识别意图中…"}}
        decision = self._safe_route(query, session, trace)
        yield {"type": "meta", "data": {
            "decision": decision.to_dict(),
            "trace": {k: v for k, v in trace.items() if k != "timings"} | {"timings": dict(trace["timings"])},
        }}

        # Run the selected tool.
        yield {"type": "status", "data": {
            "phase": "tool",
            "tool": decision.tool,
            "message": _TOOL_WORKING_HINT.get(decision.tool, "处理中…"),
        }}
        tool_result = self._safe_run_tool(decision, session, trace, raw_query=query)
        yield {"type": "tool_result", "data": tool_result.to_dict()}

        # Stream the composed response.
        if tool_result.needs_composer:
            compose_hint = _TOOL_COMPOSE_HINT.get(decision.tool) or "正在生成回复…"
            yield {"type": "status", "data": {"phase": "compose", "message": compose_hint}}
        t2 = time.perf_counter()
        narrative_parts: list[str] = []
        try:
            for piece in self._composer.compose_stream(tool_result, session):
                narrative_parts.append(piece)
                yield {"type": "token", "data": piece}
        except Exception as exc:  # noqa: BLE001 - guard unexpected stream failures
            logger.exception("Streaming composer raised unexpectedly")
            trace["composer_error"] = repr(exc)
            fallback = self._fallback_narrative(tool_result)
            if not narrative_parts:
                narrative_parts.append(fallback)
                yield {"type": "token", "data": fallback}
        trace["timings"]["composer_ms"] = int((time.perf_counter() - t2) * 1000)

        narrative = "".join(narrative_parts).strip()
        if not narrative:
            # Fall back if the stream produces no token.
            narrative = self._fallback_narrative(tool_result)
            yield {"type": "token", "data": narrative}

        session.add_assistant(narrative)

        yield {"type": "done", "data": {
            "timings": trace["timings"],
            "narrative": narrative,
            "trace": trace,
        }}

    # ----------------------------- Visual search -----------------------------

    def handle_image_turn_stream(
        self,
        image: str,
        session: AgentSession,
        hint_text: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Stream visual search: inspect image, extract terms, rerank, and compose.

        The modality is already known, so this path bypasses routing and directly uses
        recommendation. ``hint_text`` is optional text submitted with the image.
        """
        from llm.vision import UNRECOGNIZED, vision_extract_query

        trace: dict[str, Any] = {"timings": {}}
        image = (image or "").strip()
        hint_text = (hint_text or "").strip()

        # Extract Chinese retrieval terms from the image.
        yield {"type": "status", "data": {"phase": "vision", "message": "正在识别图片…"}}
        t0 = time.perf_counter()
        try:
            extracted = vision_extract_query(image)
        except Exception as exc:  # noqa: BLE001 - degrade instead of breaking the stream
            logger.exception("vision_extract_query failed")
            trace["vision_error"] = repr(exc)
            extracted = UNRECOGNIZED
        trace["timings"]["vision_ms"] = int((time.perf_counter() - t0) * 1000)

        # Store this image turn, including optional text, for composer context.
        display = f"[图片] {hint_text}".strip() if hint_text else "[图片搜索]"
        session.add_user(display)

        # Merge image and text terms so textual constraints affect retrieval.
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

        # Skip retrieval and clarify when the image cannot be recognized.
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

        # Run visual search and reranking.
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

        # Stream composition using the same contract as the text path.
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

    # ----------------------------- Internal helpers -----------------------------

    def _safe_route(
        self, query: str, session: AgentSession, trace: dict[str, Any],
    ) -> IntentDecision:
        logger.info("turn start: query=%r", query[:60])
        # While cart waits for a product or SKU selection, bypass the router so short
        # answers are not mistaken for refinement. Explicit new searches release pending.
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
        # Route selection-like answers back to a pending product-detail clarification;
        # release the state when the user changes topic.
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
        # Likewise, return selection-like answers to a pending comparison.
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
            # On router failure, deterministic keywords preserve clear cart/checkout
            # intent; other requests use recommendation as the safe default.
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
        """Apply deterministic guards for a few high-confidence intents."""
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
        """Choose a deterministic keyword fallback after a router failure.

        Only clear cart and checkout intents are special-cased; everything else falls
        back to recommendation.
        """
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
            # Tools that resolve ordinals or demonstratives must receive the raw query;
            # a rewritten product name could otherwise change the referenced item.
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
        # Local narrative used when composition fails.
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
    - Asking about one named product's battery life after a phone list -> product_detail
    - Asking which listed product has better battery life -> compare
    - Requesting tablets with good battery life -> keep recommend/refine
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


# Explicit search signals release pending product/SKU selection. Requiring a strong
# search verb while excluding demonstrative-only replies protects normal selections.
_ESCAPE_SEARCH_WORDS: tuple[str, ...] = (
    "推荐", "有没有", "有什么", "想看", "想买", "看看别的", "换成", "换个", "再找", "找找",
    "其他", "别的",
)


def _is_new_search_escape(query: str) -> bool:
    """Return whether a turn starts a new search and should release pending cart state.

    A strong search verb counts unless the query contains a focused demonstrative.
    Pure selection replies remain pending so the cart operation can finish.
    """
    text = (query or "").strip()
    if not text:
        return False
    if any(word in text for word in _DETAIL_DEICTIC_WORDS):
        return False
    return any(word in text for word in _ESCAPE_SEARCH_WORDS)
