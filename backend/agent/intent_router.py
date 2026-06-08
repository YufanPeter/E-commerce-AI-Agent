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
from llm.client import get_client, get_model_id


logger = logging.getLogger(__name__)


# 已注册的 tool 名。新增 tool 时同步更新。
# 把它写死在路由层而不是动态扫描，是为了让 enum 在 prompt 时就锁定，
# 避免运行时 tool 表变化导致 router 输出 enum 之外的值。
#
# refine/compare/product_detail 当前是档子实现，在 Step 5 里会改为
# “粗分类 LLM + 状态机细化”的两段决策，不依赖 router LLM 直接选到。
KNOWN_TOOLS: tuple[str, ...] = (
    "recommend",
    "refine",
    "compare",
    "product_detail",
    "cart",
    "clarify",
    "fallback",
)


@dataclass(frozen=True)
class IntentDecision:
    """Router 的输出：选哪个 tool，附带改写后的 query 和置信度。"""

    tool: str                   # 必属于 KNOWN_TOOLS
    rewritten_query: str        # 结合上下文改写后的 query，比如把"再便宜点"改成完整问题
    confidence: str             # "high" | "medium" | "low"
    reasoning: str              # debug 用，不展示给用户

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "rewritten_query": self.rewritten_query,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


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
5. 真正模糊到无法检索的（"随便看看"、"有啥好东西"、"今天买点啥"）才走 clarify。
6. 与购物完全无关的（天气、新闻、闲聊、技术问题）走 fallback。
7. rewritten_query 一定要结合"最近对话"上下文，让单独看也能理解。
"""


def route(query: str, session: AgentSession, timeout: float = 6.0) -> IntentDecision:
    """主入口。LLM 失败时抛异常，由 orchestrator 决定降级。"""
    user_content = _build_user_content(query, session)

    client = get_client()
    # 已强制 tool_choice，但 LLM 仍偶发抖动：① 完全不调用 route_to_tool；
    # ② 调用了但 tool 字段漏填（返回 null）。两者都靠重试一次显著压低概率。
    # 仍失败才退回 recommend——电商 70% 流量是推荐，是最安全的默认，
    # 绝不退回 clarify（会把明确的购物意图错判成"太模糊"，反问用户品类）。
    last_exc: Exception | None = None
    args: dict[str, Any] | None = None
    for attempt in range(2):
        response = client.chat.completions.create(
            model=get_model_id(),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            tools=[_TOOL_SCHEMA],
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
        # tool 字段漏填或不在已知集合内时也重试：模型常常 reasoning/rewritten_query
        # 都对、只漏了 enum 字段，重试一次往往就补上了。
        if candidate.get("tool") in KNOWN_TOOLS:
            args = candidate
            break
        last_exc = ValueError(f"Router returned invalid tool {candidate.get('tool')!r}")
        logger.warning(
            "Router returned invalid tool %r (attempt %d/2), retrying",
            candidate.get("tool"), attempt + 1,
        )
        args = candidate  # 留作兜底用（取其 rewritten_query 等字段）

    if args is None:
        raise last_exc  # type: ignore[misc]

    tool = args.get("tool")
    if tool not in KNOWN_TOOLS:
        # 重试后仍漏填：退回 recommend（购物安全默认），而非 clarify。
        logger.warning("Router tool still invalid %r after retry, defaulting to recommend", tool)
        tool = "recommend"

    return IntentDecision(
        tool=tool,
        rewritten_query=str(args.get("rewritten_query") or query).strip() or query,
        confidence=str(args.get("confidence", "medium")),
        reasoning=str(args.get("reasoning", "")),
    )


def _build_user_content(query: str, session: AgentSession) -> str:
    """把当前 query + 最近对话拼成 router 的输入。"""
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
