from __future__ import annotations

"""Intent Router：用 LLM function calling 把用户 query 分发到对应 tool。

为什么用 function calling 而不是普通 chat completion 解析 JSON：
    enum 约束能把模型输出锁死在已注册的 tool 名上，比"请输出 JSON"
    式 prompt 稳定一个数量级，不会出现 tool='recommendation' 这种
    拼写差异导致路由失败。

为什么不让 LLM 直接调底层工具（OpenAI-style tool calling）：
    电商场景 70% 是推荐，需要严格走 SearchService → rerank → composer
    的流水线。让 LLM 自己挑工具会带来：① 每轮多一次 LLM 调用；
    ② 流水线被打乱（如忘记调 rerank）；③ 难以接入流式 status 上报。
    Tool 路由本质是"让 LLM 只负责分类、固定流水线负责执行"，控制力更强。
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from agent.session import AgentSession
from llm.cache import TTLCache
from i18n import en as english
from i18n import is_english
from llm.client import get_client, get_model_id


logger = logging.getLogger(__name__)


# Registered tool names. Keep this list in sync when adding a tool. Defining it here
# fixes the prompt enum and prevents runtime registry changes from producing new values.
#
# Refine, compare, and product detail use coarse LLM routing followed by state-based
# refinement, so correctness does not depend on a single exact router decision.
KNOWN_TOOLS: tuple[str, ...] = (
    "recommend",
    "refine",
    "compare",
    "product_detail",
    "cart",
    "clarify",
    "fallback",
)


# The English catalog routes graph questions directly and reads tool arguments from the
# same call, so no rule has to re-read the message afterwards.
ENGLISH_TOOLS: tuple[str, ...] = (*KNOWN_TOOLS, "similar", "accessories", "buying_guide")
CART_ACTIONS: tuple[str, ...] = (
    "add", "remove", "set_quantity", "change_spec", "view", "checkout", "reorder", "order_history", "answer", "cancel",
)
CLEARABLE: tuple[str, ...] = ("budget", "brand", "measurements", "exclusions", "device", "features")


@dataclass(frozen=True)
class IntentDecision:
    """Router output containing the selected tool, rewritten query, and confidence."""

    tool: str                   # Must be a member of KNOWN_TOOLS.
    rewritten_query: str        # Context-expanded query, including implicit follow-ups.
    confidence: str             # "high" | "medium" | "low"
    reasoning: str              # Diagnostic only; never shown to the user.
    # English only: what the tool acts on (positions, cart action, quantity, ...), checked
    # by ``english_args``. ``None`` means the tool reads the message itself.
    args: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "tool": self.tool,
            "rewritten_query": self.rewritten_query,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }
        if self.args is not None:
            data["args"] = self.args
        return data


_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "route_to_tool",
        "description": "把用户 query 分发到合适的 tool 处理。",
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "enum": list(KNOWN_TOOLS),
                    "description": (
                        "选择最合适的 tool：\n"
                        "- recommend: 用户想找/挑选/购买商品（含'推荐'、'有什么'、'500以内的XX'）\n"
                        "- refine: 用户基于上一轮推荐要继续调整（'再便宜点'、'换个品牌'、'还有别的吗'），"
                        "也包括【只补充一个约束】的短输入（如单独说 'Adidas'、'红色的'、'轻一点'、'预算500'）\n"
                        "- compare: 用户希望对比两个以上商品（'这两个有什么区别'、'A 和 B 哪个好'）\n"
                        "- product_detail: 用户对某个具体商品要深度介绍（'第二个详细说说'、'这款敏感肌能用吗'）\n"
                        "- cart: 购物车与下单相关（'加入购物车'、'把刚才那款加进来'、'删掉第二个'、"
                        "'改成两件'、'看看购物车'、'下单吧'、'结算'）\n"
                        "- clarify: query 过于模糊无法检索（如'随便看看'、'有啥好东西'）\n"
                        "- fallback: 与购物无关的问题（天气、闲聊、超出能力范围的请求）\n"
                        "注意：refine/compare/product_detail 都需要上一轮有推荐结果，"
                        "若上下文里看不到 last_hits 提示，退回 recommend。"
                    ),
                },
                "rewritten_query": {
                    "type": "string",
                    "description": (
                        "结合最近对话改写为独立完整的查询。"
                        "例如上一轮聊'500以内的精华'，本轮说'再便宜点'，"
                        "应改写为'300以内的精华'。无需改写时原样输出。"
                    ),
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "对路由判断的置信度。模糊不清时填 low，触发 clarify。",
                },
                "reasoning": {
                    "type": "string",
                    "description": "简短解释选这个 tool 的理由（1-2 句）。",
                },
            },
            "required": ["tool", "rewritten_query", "confidence", "reasoning"],
            "additionalProperties": False,
        },
    },
}


SYSTEM_PROMPT = """你是电商导购 Agent 的路由器，唯一职责是把用户 query 分发到合适的 tool。

严格规则：
1. 必须调用 route_to_tool 函数，不要直接回答用户。
2. 购物相关一律走 recommend，即使用户用模糊词（"好看的"、"经济实惠的"）只要有品类线索就 recommend。
3. “再便宜点”/“换个”/“还有别的吗”这类基于上一轮调整的 → refine（前提上文有推荐结果）。
   特别地：当上一轮已给出推荐，而本轮只是一个【单一约束】的短词（一个品牌名、颜色、
   价格、轻重等属性，如单独的 “Adidas”、“红色”、“便宜点”），应判为 refine 而非 recommend，
   并在 rewritten_query 里补全上一轮的品类，例如上一轮“推荐跑鞋”+本轮“Adidas”→“Adidas 跑鞋”。
4. “对比”/“比较”/“哪个好”/“区别” → compare；即使用户同时点名品牌/商品
    （如“对比小米和华为这两款”“Apple 和华为哪个好”），也必须走 compare，
    不要因为包含商品名而改判 recommend。 “第 X 个详细说说”/“这款能…吗” → product_detail。
   “加入购物车/加进来/删掉/改数量/看看购物车/下单/结算” → cart（工具内部再细分动作）。
   “再买一次/上次买的再来两瓶/我买过什么/历史订单” 也是 cart。
5. 真正模糊到无法检索的（"随便看看"、"有啥好东西"、"今天买点啥"）才走 clarify。
6. 与购物完全无关的（天气、新闻、闲聊、技术问题）走 fallback。
7. rewritten_query 一定要结合"最近对话"上下文，让单独看也能理解。
"""


def _tool_schema() -> dict[str, Any]:
    """The routing function with field descriptions in the catalog's language."""
    if not is_english():
        return _TOOL_SCHEMA
    function = _TOOL_SCHEMA["function"]
    properties = function["parameters"]["properties"]
    fields = english.ROUTER_ARG_FIELDS
    nullable_int = {"type": ["integer", "null"]}
    english_properties = {
        "tool": {"type": "string", "enum": list(ENGLISH_TOOLS), "description": english.ROUTER_TOOL_FIELD},
        "rewritten_query": {**properties["rewritten_query"], "description": english.ROUTER_REWRITE_FIELD},
        "confidence": {**properties["confidence"], "description": "How certain the routing is; use low when unclear."},
        "reasoning": {**properties["reasoning"], "description": "One short sentence."},
        "positions": {"type": "array", "items": {"type": "integer"}, "description": fields["positions"]},
        "quantity": {**nullable_int, "description": fields["quantity"]},
        "cart_action": {"type": ["string", "null"], "enum": [*CART_ACTIONS, None], "description": fields["cart_action"]},
        "cart_item": {**nullable_int, "description": fields["cart_item"]},
        "size_step": {"type": "integer", "enum": [-1, 0, 1], "description": fields["size_step"]},
        "clear": {"type": "array", "items": {"type": "string", "enum": list(CLEARABLE)}, "description": fields["clear"]},
        "cheaper": {"type": "boolean", "description": fields["cheaper"]},
        "whole_order": {"type": "boolean", "description": fields["whole_order"]},
        "reorder_item": {"type": "string", "description": fields["reorder_item"]},
    }
    return {
        "type": "function",
        "function": {
            **function,
            "description": "Route the shopper's message to the right tool and read what it acts on.",
            "parameters": {
                "type": "object",
                "properties": english_properties,
                "required": list(english_properties),
                "additionalProperties": False,
            },
        },
    }


def english_args(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only well-formed tool arguments; anything else reads as "not said"."""

    def positive(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 100 else None

    action = raw.get("cart_action")
    step = raw.get("size_step")
    return {
        "positions": list(dict.fromkeys(p for p in (positive(v) for v in raw.get("positions") or []) if p)),
        "quantity": positive(raw.get("quantity")),
        "cart_action": action if action in CART_ACTIONS else None,
        "cart_item": positive(raw.get("cart_item")),
        "size_step": step if step in (-1, 0, 1) else 0,
        "clear": [c for c in dict.fromkeys(raw.get("clear") or []) if c in CLEARABLE],
        "cheaper": raw.get("cheaper") is True,
        "whole_order": raw.get("whole_order") is True,
        "reorder_item": " ".join(str(raw.get("reorder_item") or "").split()),
    }


# 首轮路由缓存：没有对话历史时，路由结果只取决于 query 本身，
# 同一句话（demo 热词 / 用户重试）不必再打一次 LLM。
_FIRST_TURN_CACHE: TTLCache[IntentDecision] = TTLCache(maxsize=512)


def clear_route_cache() -> None:
    _FIRST_TURN_CACHE.clear()


def route(query: str, session: AgentSession, timeout: float = 6.0) -> IntentDecision:
    """主入口。LLM 失败时抛异常，由 orchestrator 决定降级。"""
    stateless = not session.history_for_router() and not (is_english() and _english_state(session))
    cache_key = " ".join((query or "").split()) if stateless else None
    if cache_key:
        cached = _FIRST_TURN_CACHE.get(cache_key)
        if cached is not None:
            logger.info("router cache hit → tool=%s", cached.tool)
            return cached
    decision = _route_uncached(query, session, timeout)
    if cache_key:
        _FIRST_TURN_CACHE.set(cache_key, decision)
    return decision


def _route_uncached(query: str, session: AgentSession, timeout: float) -> IntentDecision:
    user_content = _build_user_content(query, session)

    client = get_client()
    # Even with a forced tool choice, the model may omit the function call or tool field.
    # One retry handles most cases; recommendation is the safest final shopping default.
    last_exc: Exception | None = None
    args: dict[str, Any] | None = None
    tools = ENGLISH_TOOLS if is_english() else KNOWN_TOOLS
    for attempt in range(2):
        response = client.chat.completions.create(
            model=get_model_id(role="fast"),
            messages=[
                {"role": "system", "content": english.ROUTER_SYSTEM if is_english() else SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            tools=[_tool_schema()],
            tool_choice={"type": "function", "function": {"name": "route_to_tool"}},
            temperature=0.1,
            timeout=timeout,
        )
        try:
            candidate = _extract_tool_arguments(response)
        except ValueError as exc:
            last_exc = exc
            logger.warning("Router empty response (attempt %d/2), retrying", attempt + 1)
            continue
        # Retry a missing or unknown tool field; the rest of the response is often valid.
        if candidate.get("tool") in tools:
            args = candidate
            break
        last_exc = ValueError(f"Router returned invalid tool {candidate.get('tool')!r}")
        logger.warning(
            "Router returned invalid tool %r (attempt %d/2), retrying",
            candidate.get("tool"), attempt + 1,
        )
        args = candidate  # Retain partial fields for fallback.

    if args is None:
        raise last_exc  # type: ignore[misc]

    tool = args.get("tool")
    if tool not in tools:
        # After retry exhaustion, prefer recommendation over unnecessary clarification.
        logger.warning("Router tool still invalid %r after retry, defaulting to recommend", tool)
        tool = "recommend"

    return IntentDecision(
        tool=tool,
        rewritten_query=str(args.get("rewritten_query") or query).strip() or query,
        confidence=str(args.get("confidence", "medium")),
        reasoning=str(args.get("reasoning", "")),
        args=english_args(args) if is_english() else None,
    )


def _numbered(items: list[dict[str, Any]]) -> str:
    return "\n".join(f"{i}. {str(item.get('title') or '')[:90]}" for i, item in enumerate(items, start=1))


def _english_state(session: AgentSession) -> list[str]:
    """What the shopper can point at, so the router can read positions and answers."""
    task = session.task
    view = task.active_view
    titles = {item.get("product_id"): item.get("title") for item in [*view.items, *task.shopping_items]}
    sections = []
    if view.kind == "cart" and view.items:
        sections.append(f"Cart on screen (cart_item counts these):\n{_numbered(view.items)}")
        if task.shopping_items:
            sections.append(f"Earlier results (positions count these):\n{_numbered(task.shopping_items)}")
    elif view.items:
        sections.append(f"Products on screen (positions count these):\n{_numbered(view.items)}")
    if task.focus_product_id in titles:
        position = next((i for i, item in enumerate(task.add_targets(), start=1) if item.get("product_id") == task.focus_product_id), None)
        sections.append(f"Focused product: {f'{position}. ' if position else ''}{titles[task.focus_product_id]}")
    if task.last_added:
        sections.append(f"Just added to the cart: {titles.get(task.last_added.get('product_id')) or 'a product'}")
    pending = task.pending
    if pending is not None and pending.kind == "cart_sku":
        sections.append(f"Waiting for the shopper to choose a size, color or other option of {pending.data.get('title')} to add.")
    elif pending is not None and pending.kind == "cart_pick":
        choices = _numbered(pending.data.get("candidates") or [])
        sections.append(f"Waiting for the shopper to say which product to add:\n{choices}")
    return sections


def _build_user_content(query: str, session: AgentSession) -> str:
    """Combine the current query with recent conversation context."""
    if is_english():
        parts = [f"Recent conversation:\n{session.recent_text(n=4)}"] if session.history_for_router() else []
        parts += _english_state(session)
        return "\n\n".join([*parts, f"Current query: {query}"])
    if not session.history:
        return f"当前 query：{query}"
    return (
        f"最近对话：\n{session.recent_text(n=4)}\n\n"
        f"当前 query：{query}"
    )


def _extract_tool_arguments(response: Any) -> dict[str, Any]:
    message = response.choices[0].message
    if not message.tool_calls:
        raise ValueError("Router LLM 没有调用 route_to_tool，原始响应：" + str(message))
    raw = message.tool_calls[0].function.arguments
    return json.loads(raw)
