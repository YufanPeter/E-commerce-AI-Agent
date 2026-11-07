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

import json
import logging
import os
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
from agent.tools.reference import has_relative_reference, resolve_by_name, resolve_indices
from agent.tools.recommend import RecommendTool
from agent.tools.refine import RefineTool, parse_more_request


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
#   narrative:   composer 的结构化流式片段：{"part": "opening"|"opening_end"|"group"|"item"|"summary"|"followup", ...}
#                开场白逐字到、商品解说一款一款到、总结和追问最后到——前端按到达顺序弹出
#   token:       一段文本片段（纯文本工具逐 token；结构化工具在末尾再补一份完整 JSON 兼容旧客户端）
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

# 各工具对应的"正在工作中"提示文案，前端显示成思考步骤
_THINKING_STEP = "思考中…"
_TOOL_WORKING_HINT = {
    "recommend": "正在搜索商品…",
    "refine": "正在重新筛选…",
    "compare": "正在对比商品…",
    "product_detail": "正在查看商品详情…",
    "cart": "正在处理购物车…",
    "clarify": "正在整理追问…",
    "fallback": "正在生成回复…",
}
_STEP_QUERY_MAX_CHARS = 24
# 口语开头对"正在搜索 XX"这句话没有信息量，去掉后只留要找的东西
_STEP_QUERY_FILLER_RE = re.compile(
    r"^(你好|您好|请|麻烦)?(帮我|给我|我想要|我想|我要|想要)?(推荐|找|搜索|搜|看看|买)?(一下|几款|一些|一款|几个)?"
)

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

        if _should_prefetch(query, session):
            self._prefetch_search_understanding(query)
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
        """流式版：按顺序 yield meta/tool_result/token*/done。调用方提前关闭时记录半截回合。"""
        return _capture_interrupt(self._turn_events(query, session), session)

    def _turn_events(
        self,
        query: str,
        session: AgentSession,
    ) -> Iterator[dict[str, Any]]:
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

        # ① 路由（同时后台预取 query 解析 / 多需求拆解，和 router 并行）
        yield {"type": "status", "data": {"phase": "routing", "message": _THINKING_STEP}}
        if _should_prefetch(query, session):
            self._prefetch_search_understanding(query)
        decision = self._safe_route(query, session, trace)
        yield {"type": "meta", "data": {
            "decision": decision.to_dict(),
            "trace": {k: v for k, v in trace.items() if k != "timings"} | {"timings": dict(trace["timings"])},
        }}

        # ② 工具
        yield {"type": "status", "data": {
            "phase": "tool",
            "tool": decision.tool,
            "message": _tool_step_text(decision, query),
        }}
        tool_result = self._safe_run_tool(decision, session, trace, raw_query=query)
        yield {"type": "tool_result", "data": tool_result.to_dict()}

        # ③ Composer：流式（结构化事件）
        if tool_result.needs_composer:
            yield {"type": "status", "data": {"phase": "compose", "message": _compose_step_text(tool_result)}}
        t2 = time.perf_counter()
        narrative = yield from self._stream_composer(tool_result, session, trace)
        trace["timings"]["composer_ms"] = int((time.perf_counter() - t2) * 1000)

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
        """图片输入的流式入口，提前关闭时的处理与文本链路一致。"""
        return _capture_interrupt(self._image_turn_events(image, session, hint_text), session)

    def _image_turn_events(
        self,
        image: str,
        session: AgentSession,
        hint_text: str = "",
    ) -> Iterator[dict[str, Any]]:
        """图片输入的流式入口：看图 → 抽检索词 → 视觉重排检索 → composer 流式。

        模态已知（就是图搜），无需 router LLM 分类，直接走 recommend 的图搜路径。
        ``hint_text`` 是用户随图附带的文字（图+文场景，可空）。
        """
        from llm.vision import UNRECOGNIZED, parsed_query_from_phrase, vision_extract_query

        trace: dict[str, Any] = {"timings": {}}
        image = (image or "").strip()
        hint_text = (hint_text or "").strip()

        # ① 看图：把图片抽成中文检索词。图片向量只依赖原图，和 VLM 看图并行算，
        #    后面视觉重排直接用现成结果（省一次 embedding 往返）。
        yield {"type": "status", "data": {"phase": "vision", "message": "正在识别图片…"}}
        embedding_future = _prefetch_image_embedding(image)
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
        # 纯图搜：检索意图由 VLM 短语确定性构造（品类按 taxonomy 匹配），跳过 2s 的
        # 文本 query 解析。图+文：附带文字可能带价格 / 否定等约束，仍交给 query 解析。
        prebuilt_parsed = None
        if extracted != UNRECOGNIZED and hint_text:
            effective_query = f"{extracted} {hint_text}"
        elif extracted != UNRECOGNIZED:
            try:
                prebuilt_parsed = parsed_query_from_phrase(extracted)
            except Exception as exc:  # noqa: BLE001 - 构造失败就走原来的 LLM 解析
                logger.warning("parsed_query_from_phrase failed: %r", exc)

        decision = IntentDecision(
            tool="recommend",
            rewritten_query=effective_query,
            confidence="high",
            reasoning="image visual search",
        )
        yield {"type": "meta", "data": {
            "decision": decision.to_dict(),
            "trace": {
                "timings": dict(trace["timings"]),
                "extracted_query": extracted,
                "vision_parsed": prebuilt_parsed.to_dict() if prebuilt_parsed is not None else None,
            },
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
            try:
                tool_result = recommend.run_image(
                    effective_query, image, session,
                    image_embedding=embedding_future, parsed=prebuilt_parsed,
                )
            except TypeError:
                # 测试 stub / 老实现不认识 image_embedding 参数
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
            yield {"type": "status", "data": {"phase": "compose", "message": _compose_step_text(tool_result)}}
        t2 = time.perf_counter()
        narrative = yield from self._stream_composer(tool_result, session, trace)
        trace["timings"]["composer_ms"] = int((time.perf_counter() - t2) * 1000)

        session.add_assistant(narrative)
        yield {"type": "done", "data": {
            "timings": trace["timings"],
            "narrative": narrative,
            "trace": trace,
        }}

    # ----------------------------- 内部 -----------------------------

    def _stream_composer(
        self,
        tool_result: ToolResult,
        session: AgentSession,
        trace: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """把 composer 的结构化事件转成对外事件；返回最终 narrative（generator return）。

        事件顺序：narrative*（开场白 delta → 商品解说逐款 → 追问逐条）/ token* →
        末尾再发一次完整 narrative 的 token（结构化工具是 JSON），旧客户端据此渲染，
        新客户端若已收到 narrative 片段可忽略它。"""
        narrative = ""
        structured_seen = False
        events = None
        try:
            events = self._composer.compose_events(tool_result, session)
            for ev in events:
                kind = ev.get("type")
                if kind == "final":
                    narrative = (ev.get("data") or "").strip()
                    break
                if kind == "narrative":
                    structured_seen = True
                yield ev
        except Exception as exc:  # noqa: BLE001 - 兜底，理论 compose_events 已经吞了
            logger.exception("Streaming composer raised unexpectedly")
            trace["composer_error"] = repr(exc)
        finally:
            close_events = getattr(events, "close", None)
            if close_events is not None:
                close_events()
        if not narrative:
            # 极端情况下流没产出任何内容，给个兜底
            narrative = self._fallback_narrative(tool_result)
            yield {"type": "token", "data": narrative}
            return narrative
        if structured_seen:
            # 兼容：旧客户端只认 token 里的 JSON；新客户端收到 narrative 片段后会忽略这条。
            yield {"type": "token", "data": narrative}
        elif tool_result.narrative_override is None and tool_result.needs_composer:
            # 纯文本工具（product_detail）已经逐 token 发过了，这里不再重复
            pass
        return narrative

    @staticmethod
    def _prefetch_search_understanding(query: str) -> None:
        """在 router LLM 还在跑时，并行预取检索链路里的两次 LLM 调用。

        首轮 / refine 的 rewritten_query 大多等于原句，预取结果直接命中缓存，
        把"router → 解析 → 拆解"三段串行压成一段。猜错时只多花一次调用，不加延迟。"""
        try:
            from search.query_decomposer import prefetch_decompose
            from search.query_understanding import prefetch_understand

            prefetch_understand(query)
            prefetch_decompose(query)
        except Exception as exc:  # noqa: BLE001 - 预取只是加速，失败不影响主链路
            logger.debug("prefetch skipped: %r", exc)

    def _safe_route(
        self, query: str, session: AgentSession, trace: dict[str, Any],
    ) -> IntentDecision:
        logger.info("turn start: query=%r", query[:60])
        # 若上一轮 cart 正在等用户选规格或选商品，则本轮强制走 cart，跳过 router LLM——
        # 否则"暗夜黑 42码""第一个"这类回答会被路由误判为 refine/recommend。
        # 例外：用户明确改口开新检索（“推荐几款笔记本”“有没有便宜点的平板”），这类话
        # 带检索动词、不会是规格应答，应释放 pending 交回正常路由，而不是硬塞进 cart。
        more_label = parse_more_request(query)
        if session.get("pending_cart") or session.get("pending_add"):
            if _is_new_search_escape(query) or more_label is not None:
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
        # "看更多"的意思是确定的，走 refine 沿用上一轮条件，不用再问 router。
        if more_label is not None and session.recall_parsed():
            decision = IntentDecision(
                tool="refine",
                rewritten_query=more_label or query,
                confidence="high",
                reasoning="see more of previous results",
            )
            trace["timings"]["router_ms"] = 0
            trace["decision"] = decision.to_dict()
            logger.info("router skipped: see more → tool=refine")
            return decision
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
        # 「上一个 / 下一个呢」是相对当前聚焦商品的指代，不是让我重新推荐。
        if (
            decision.tool in {"recommend", "refine", "clarify"}
            and has_relative_reference(raw_text)
            and session.get("last_focus_product_id")
            and session.recall_hits()
        ):
            return IntentDecision(
                tool="product_detail",
                rewritten_query=query,
                confidence="high",
                reasoning=f"post-route guard: relative reference to focused product; original={decision.tool}",
            )
        # 上一轮刚出对比表，这句只是换规格（「换成 512G 的」）→ 留在 compare 重算
        last_cmp = session.get("last_comparison")
        if last_cmp and decision.tool in {"recommend", "refine", "product_detail", "clarify"}:
            from agent.tools.compare import is_spec_switch_reply

            if is_spec_switch_reply(raw_text, last_cmp):
                return IntentDecision(
                    tool="compare",
                    rewritten_query=query,
                    confidence="high",
                    reasoning=f"post-route guard: spec switch on last comparison; original={decision.tool}",
                )
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
            slots: dict[str, Any] = {}
            more_label = parse_more_request(raw_query)
            if decision.tool == "refine" and more_label is not None and session.recall_parsed():
                slots["more_label"] = more_label
            tool_result = tool.run(tool_query, session, slots=slots)
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


_IMAGE_EXECUTOR: Any | None = None


def _tool_step_text(decision: IntentDecision, raw_query: str) -> str:
    """工具开始前的步骤文案。此时结构化解析还没出来，用 router 改写后的查询说明在找什么。"""
    if decision.tool not in {"recommend", "refine"}:
        return _TOOL_WORKING_HINT.get(decision.tool, "处理中…")
    more_label = parse_more_request(raw_query)
    if more_label is not None:
        return f"正在找更多{more_label}…" if more_label else "正在找更多商品…"
    phrase = _STEP_QUERY_FILLER_RE.sub("", (decision.rewritten_query or "").strip(), count=1)
    phrase = phrase.strip().rstrip("？?。.!！~～ 吧吗呢")
    if not phrase:
        return _TOOL_WORKING_HINT[decision.tool]
    if len(phrase) > _STEP_QUERY_MAX_CHARS:
        phrase = phrase[:_STEP_QUERY_MAX_CHARS] + "…"
        return f"正在搜索{phrase}"
    return f"正在搜索{phrase}…"


def _filter_summary(parsed: dict[str, Any]) -> str:
    """把解析出的硬条件拼成一句人能读的话，让用户看到系统是怎么理解需求的。"""
    parts: list[str] = []
    target = parsed.get("sub_category") or parsed.get("category")
    if target:
        parts.append(str(target))
    brands = parsed.get("brand_include") or []
    if brands:
        parts.append("、".join(str(b) for b in brands))
    low, high = parsed.get("min_price"), parsed.get("max_price")
    if low is not None and high is not None:
        parts.append(f"{low:g}-{high:g} 元")
    elif high is not None:
        parts.append(f"{high:g} 元以内")
    elif low is not None:
        parts.append(f"{low:g} 元以上")
    avoid = [*(parsed.get("negative_ingredients") or []), *(parsed.get("brand_exclude") or [])]
    if avoid:
        parts.append("不要" + "、".join(str(a) for a in avoid))
    return " · ".join(parts)


def _compose_step_text(tool_result: ToolResult) -> str:
    """工具结束后的步骤文案：说清实际找到了什么，只说发生过的事。"""
    if tool_result.tool_name not in {"recommend", "refine"}:
        return _TOOL_COMPOSE_HINT.get(tool_result.tool_name) or "正在生成回复…"
    payload = tool_result.payload or {}
    count = len(payload.get("products") or [])
    if not count:
        return "没有找到完全符合的，正在想替代方向…"
    parsed = (payload.get("debug") or {}).get("parsed")
    filters = _filter_summary(parsed) if isinstance(parsed, dict) else ""
    found = f"按「{filters}」找到 {count} 款" if filters else f"找到 {count} 款"
    return f"{found}，正在写推荐理由…"


_INTERRUPTED_EMPTY_NOTE = "（用户中断了这轮回答，尚未给出结果）"
_INTERRUPTED_TEXT_SUFFIX = "（回答被用户中断）"


class _TurnProgress:
    """记录本轮已经发给客户端的内容，中断时据此写入历史和工作记忆。"""

    def __init__(self, prior_hits: list[Any]) -> None:
        self._prior_hits = prior_hits
        self._opening = ""
        self._items: list[dict[str, str]] = []
        self._summary = ""
        self._tokens: list[str] = []
        self._structured = False

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        data = event.get("data")
        if kind == "token" and isinstance(data, str):
            self._tokens.append(data)
        if kind != "narrative" or not isinstance(data, dict):
            return
        self._structured = True
        part = data.get("part")
        if part == "opening":
            self._opening += data.get("delta") or ""
        elif part == "opening_end":
            self._opening = data.get("text") or self._opening
        elif part == "item" and data.get("productId"):
            item = {"productId": data["productId"], "description": data.get("description") or ""}
            if data.get("group"):
                item["group"] = data["group"]
            self._items.append(item)
        elif part == "summary":
            self._summary = data.get("text") or ""

    def commit(self, session: AgentSession) -> None:
        """用户消息还没有回答时才写入，已正常结束的回合不受影响。"""
        if not session.history or session.history[-1].role != "user":
            return
        session.add_assistant(self._narrative())
        self._align_last_hits(session)

    def align_order_with_display(self, session: AgentSession) -> None:
        """模型分组时会调整商品顺序，"第一个"要按屏幕上的顺序算。

        没有解说的商品客户端会排在最后，这里保持一致。"""
        hits = session.recall_hits()
        by_id = {hit.get("product_id"): hit for hit in hits}
        shown = [by_id[item["productId"]] for item in self._items if item["productId"] in by_id]
        if not shown:
            return
        shown_ids = {hit.get("product_id") for hit in shown}
        rest = [hit for hit in hits if hit.get("product_id") not in shown_ids]
        session.working_memory["last_hits"] = shown + rest

    def _narrative(self) -> str:
        if self._structured:
            structured: dict[str, Any] = {"opening": self._opening, "items": self._items, "followup": []}
            if self._summary:
                structured["summary"] = self._summary
            structured["interrupted"] = True
            return json.dumps(structured, ensure_ascii=False)
        text = "".join(self._tokens).strip()
        return f"{text}{_INTERRUPTED_TEXT_SUFFIX}" if text else _INTERRUPTED_EMPTY_NOTE

    def _align_last_hits(self, session: AgentSession) -> None:
        """让"第二个"指向用户实际看到的第二个：一张卡都没发就恢复上一轮的引用。
        没展示出来的商品也不算看过，之后"看更多"还能出现。"""
        by_id = {hit.get("product_id"): hit for hit in session.recall_hits()}
        shown = [by_id[item["productId"]] for item in self._items if item["productId"] in by_id]
        shown_ids = {hit.get("product_id") for hit in shown}
        unshown_ids = {pid for pid in by_id if pid not in shown_ids}
        if "seen_product_ids" in session.working_memory:
            session.working_memory["seen_product_ids"] = [
                pid for pid in session.recall_seen() if pid not in unshown_ids
            ]
        if not self._items:
            session.working_memory["last_hits"] = self._prior_hits
        elif shown:
            session.working_memory["last_hits"] = shown


def _capture_interrupt(
    events: Iterator[dict[str, Any]],
    session: AgentSession,
) -> Iterator[dict[str, Any]]:
    """转发回合事件。调用方提前关闭生成器时，把已发出的部分记入会话。"""
    progress = _TurnProgress(prior_hits=list(session.recall_hits()))
    try:
        for event in events:
            progress.observe(event)
            if event.get("type") == "done":
                progress.align_order_with_display(session)
            yield event
    except GeneratorExit:
        progress.commit(session)
        raise
    finally:
        events.close()


def _prefetch_image_embedding(image: str) -> Any | None:
    """后台计算图片向量；失败不抛（视觉重排会自行降级）。返回 Future 或 None。"""
    global _IMAGE_EXECUTOR
    if os.getenv("LLM_PREFETCH", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    try:
        from concurrent.futures import ThreadPoolExecutor
        from llm.vision import embed_image_with_provider

        if _IMAGE_EXECUTOR is None:
            _IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="img-embed")
        return _IMAGE_EXECUTOR.submit(embed_image_with_provider, image)
    except Exception as exc:  # noqa: BLE001
        logger.debug("image embedding prefetch skipped: %r", exc)
        return None


def _should_prefetch(query: str, session: AgentSession) -> bool:
    """只在大概率要走检索的回合预取：pending 态（选规格/选商品）和极短指代应答不预取。"""
    if os.getenv("LLM_PREFETCH", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if session.get("pending_cart") or session.get("pending_add"):
        return False
    text = (query or "").strip()
    if len(text) < 2:
        return False
    if any(word in text for word in _DETAIL_DEICTIC_WORDS):
        return False
    # "看更多"沿用上一轮的解析结果，不需要预取
    if parse_more_request(text) is not None and session.recall_parsed():
        return False
    return True


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

