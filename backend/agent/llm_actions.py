from __future__ import annotations

"""统一工具调用层：把"一句话 → 选哪个动作 + 抽哪些参数"收敛成一处。

为什么需要它：
    intent_router 解决的是"粗分类"——把 query 分到 recommend/compare/cart 等
    **大工具**。但像购物车这样的工具内部还有多个**子动作**（加车/改数量/删除/
    下单），每个动作要抽不同参数（"删掉第二个"要序号，"下单地址用默认的"要地址）。
    若让每个 tool 自己写一套正则去 parse，会重复、脆弱、难维护。

    这一层用 LLM function calling 做"动作分发"：调用方声明一组 ActionSpec
    （动作名 + 描述 + 参数 schema），传入 query 和最近对话，LLM 返回选中的
    动作和结构化参数。与 intent_router 同构（都用 Ark function calling + enum
    锁定），但面向"工具内动作"这一层，可被任意多动作 tool 复用。

    纯分发，不执行——执行仍由各 tool 自己用确定性代码做（DB 写入等），
    保证副作用可控、可测。LLM 只负责"听懂用户要干啥"。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent.session import AgentSession
from llm.client import get_client, get_model_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionSpec:
    """一个可被 LLM 选择的动作声明。

    name:        动作标识（被 enum 锁定，返回值一定是其中之一）。
    description: 给 LLM 的动作说明（什么场景选它）。
    parameters:  该动作要抽取的参数，JSON-Schema 的 properties 片段。
                 例：{"index": {"type": "integer", "description": "第几个，从1开始"}}
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionDecision:
    """分发结果：选了哪个动作 + 抽到的参数。"""

    action: str
    args: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)


def dispatch_action(
    query: str,
    session: AgentSession,
    actions: list[ActionSpec],
    *,
    purpose: str,
    timeout: float = 6.0,
) -> ActionDecision:
    """让 LLM 在 actions 里选一个并抽参。失败时抛异常，由调用方降级。

    purpose: 一句话说明这个工具是干嘛的，拼进 system prompt 给 LLM 语境
             （如"管理用户的购物车并完成下单"）。
    """
    schema = _build_schema(actions)
    system = (
        f"你是电商导购 Agent 的动作分发器，职责是把用户这句话映射到一个具体动作。"
        f"当前工具用途：{purpose}。"
        "必须调用 choose_action 函数，按用户真实意图选择动作并抽取参数；"
        "无法从话里确定的参数就省略，不要编造。"
    )
    user_content = _build_user_content(query, session)

    client = get_client()
    response = client.chat.completions.create(
        model=get_model_id(),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        tools=[schema],
        tool_choice={"type": "function", "function": {"name": "choose_action"}},
        temperature=0.0,
        timeout=timeout,
    )

    args = _extract_arguments(response)
    action = args.get("action")
    valid = {a.name for a in actions}
    if action not in valid:
        raise ValueError(f"动作分发返回未知 action={action!r}，期望 {valid}")
    # 把动作专属参数从顶层平铺里拆出来（schema 把它们和 action 放同级）
    params = {k: v for k, v in args.items() if k != "action" and v is not None}
    return ActionDecision(action=action, args=params, raw=args)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _build_schema(actions: list[ActionSpec]) -> dict[str, Any]:
    """把 ActionSpec 列表编成单个 function 的 JSON-Schema。

    所有动作的参数合并到同一层 properties 下（各自可选），action 用 enum 锁定。
    这样一次 function call 既选了动作、又抽了参数，省一轮往返。
    """
    properties: dict[str, Any] = {
        "action": {
            "type": "string",
            "enum": [a.name for a in actions],
            "description": "选中的动作：\n" + "\n".join(
                f"- {a.name}: {a.description}" for a in actions
            ),
        }
    }
    for spec in actions:
        for param_name, param_schema in spec.parameters.items():
            # 同名参数被多个动作共用时，沿用第一个声明即可
            properties.setdefault(param_name, param_schema)

    return {
        "type": "function",
        "function": {
            "name": "choose_action",
            "description": "选择并执行一个工具内动作。",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    }


def _build_user_content(query: str, session: AgentSession) -> str:
    if not session.history:
        return f"当前 query：{query}"
    return f"最近对话：\n{session.recent_text(n=4)}\n\n当前 query：{query}"


def _extract_arguments(response: Any) -> dict[str, Any]:
    message = response.choices[0].message
    if not message.tool_calls:
        raise ValueError("动作分发 LLM 未调用 choose_action：" + str(message))
    return json.loads(message.tool_calls[0].function.arguments)
