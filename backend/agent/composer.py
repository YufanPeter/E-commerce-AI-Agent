from __future__ import annotations

"""AnswerComposer：把 ToolResult 转成自然语言回答。

提供两种入口：
    - compose():        一次性返回完整 narrative（保留给后端测试 / 内部脚本）
    - compose_stream(): 流式生成器，逐 chunk yield 文本片段（SSE / CLI 用）

为什么独立成模块（而不是塞进 tool 里）：
    - tool 关心"做了什么"（payload），composer 关心"怎么说出来"。
      分开后可以无侵入地换风格（专业 / 活泼 / 简洁）。
    - 流式输出只需改 composer，tool 完全不动。
    - 复用：将来 compare / detail_qa tool 也走同一个 composer。
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from llm.client import get_client, get_model_id


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一位友好、专业的电商导购助手，说话像门店里真正懂行的导购，而不是念商品参数的机器人。

风格要求：
- 中文回答，简洁自然，避免冗长，控制在 200 字以内。
- 直接基于提供的 payload 中的真实商品信息说话，绝不编造任何不存在的字段或商品。
- 必须返回 JSON 格式，包含以下字段：
  - "opening": 开场白，用"为你XXX了"的格式（如"为你推荐了几款适合敏感肌的护肤品"）
  - "items": 数组，每个元素包含 {"productId": "...", "description": "..."}，**productId 必须原样使用 payload.hits 里对应商品的 productId，禁止自造序号**；每个商品必须对应一个专属解说词，不能多个商品共用一段描述
  - "questions": 数组，包含 3-5 个追问方向（如"需要更平价的选择？"）
- 报价格时一律使用 payload.hits 里的 price_display 字段（它已是「¥X 起」或「¥X」的正确形式），原样引用；不要自己拼价格、不要去掉「起」字，更不要给多规格商品报一个单一最高价。若某商品没有 price_display 则可不提价格。
- 介绍每款时落到使用场景和人群（通勤、学习、送礼…），而不是只报价格和参数。
- 适度点出关键差异帮用户决策（价格梯度、核心卖点、适合谁）。
- 若 payload.hits 为空，坦诚告知未找到，并给出具体的放宽建议（提高预算到 X、放宽品牌等）。此时 opening 说明未找到，items 为空数组。
- 若 payload 里有 groups（用户一次提了多个需求，如"衣服和防晒"），请【按组分段】介绍：每组先点明需求（如"防晒方面"、"衣服方面"），再说该组挑了哪几款、为什么适合，不要把不同需求的商品混在一起说。
- 不要复述 JSON 字段名，用自然口语介绍。
- JSON 格式必须严格正确，不要包含任何额外文字。"""


# Few-shot 示例：用真实对话演示"导购口吻"远比文字规则有效。
# 模型会直接模仿 assistant 的归并表达、场景化卖点和总括开场。
# 注意：示例里的商品/价格是虚构的演示数据，仅用于教格式，不会进入真实回答。
_FEW_SHOT: list[dict[str, str]] = [
    {
        "role": "user",
        "content": (
            'tool: recommend\n'
            'payload: {"query": "5000元以内高性价比数码", "parsed": {"category": "数码电子", "max_price": 5000}, '
            '"hits": [{"productId": "p_digital_001", "title": "华为 FreeBuds Pro 5 降噪耳机", "price": 1699}, '
            '{"productId": "p_digital_002", "title": "vivo Pad 6 Pro", "price": 3299}, {"productId": "p_digital_003", "title": "小米平板 8 Pro", "price": 3299}, '
            '{"productId": "p_digital_004", "title": "iPad Air M4", "price": 4799}, {"productId": "p_digital_005", "title": "华为 MatePad Pro Max 12.6", "price": 4999}]}'
        ),
    },
    {
        "role": "assistant",
        "content": (
            '{"opening": "为你推荐了 5 款 5000 元以内的高性价比数码", '
            '"items": ['
            '{"productId": "p_digital_001", "description": "华为 FreeBuds Pro 5（1699元）：音质和降噪都在线，通勤佩戴很舒服"}, '
            '{"productId": "p_digital_002", "description": "vivo Pad 6 Pro（3299元）：性能够用、学习娱乐两不误，性价比很高"}, '
            '{"productId": "p_digital_003", "description": "小米平板 8 Pro（3299元）：性能均衡，适合日常使用"}, '
            '{"productId": "p_digital_004", "description": "iPad Air M4（4799元）：M4芯片，性能强劲，适合专业用途"}, '
            '{"productId": "p_digital_005", "description": "华为 MatePad Pro Max（4999元）：12.6英寸大屏，办公体验极佳"}'
            '], '
            '"questions": ["需要更平价的选择？", "想要看特定品牌？", "需要详细对比某两款？"]}'
        ),
    },
    {
        "role": "user",
        "content": (
            'tool: recommend\n'
            'payload: {"query": "300元以内的蓝牙耳机", "parsed": {"sub_category": "真无线耳机", "max_price": 300}, '
            '"hits": []}\n'
            'hint: no_hits'
        ),
    },
    {
        "role": "assistant",
        "content": (
            '{"opening": "抱歉，300元以内的真无线耳机暂时没有合适的货", '
            '"items": [], '
            '"questions": ["预算能提高到多少？", "可以放宽品牌限制吗？", "考虑有线耳机吗？"]}'
        ),
    },
]


# payload 里塞 LLM 不需要的字段（如 evidence chunk）只会浪费 token。
# 这里裁剪只保留对话术有用的精简字段。
_HIT_KEEP_KEYS = ("title", "brand", "category", "sub_category", "price_display", "price", "base_price", "score")


def _trim_hit_for_llm(h: dict[str, Any]) -> dict[str, Any]:
    """裁剪单条商品，保留话术字段并把 product_id 统一成 productId 供 LLM 回写。"""
    out: dict[str, Any] = {}
    pid = h.get("product_id") or h.get("productId")
    if pid:
        out["productId"] = pid
    for k in _HIT_KEEP_KEYS:
        if k in h:
            out[k] = h[k]
    return out


def _trim_payload_for_llm(payload: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {
        "query": payload.get("query"),
    }
    # parsed 现在收纳在 debug 块；老格式（顶层 parsed）也兼容
    parsed = (payload.get("debug") or {}).get("parsed") or payload.get("parsed") or {}
    trimmed["parsed"] = {
        k: parsed.get(k)
        for k in ("category", "sub_category", "max_price", "min_price", "brand_include", "brand_exclude", "negative_ingredients")
        if parsed.get(k)
    }
    # 商品列表：新格式叫 products，老格式叫 hits
    hits = payload.get("products") or payload.get("hits") or []
    trimmed["hits"] = [_trim_hit_for_llm(h) for h in hits]
    # 多需求场景：透传分组结构，让 composer 按需求分组介绍。
    # 每组只保留 label + 精简商品，避免把 query/空组等噪声塞进 prompt。
    groups = payload.get("groups")
    if isinstance(groups, list):
        trimmed_groups = []
        for g in groups:
            g_products = g.get("products") or []
            if not g_products:
                continue
            trimmed_groups.append(
                {
                    "label": g.get("label"),
                    "hits": [_trim_hit_for_llm(h) for h in g_products],
                }
            )
        if trimmed_groups:
            trimmed["groups"] = trimmed_groups
    return trimmed


def _build_messages(tool_result: ToolResult) -> list[dict[str, str]]:
    trimmed = _trim_payload_for_llm(tool_result.payload)
    user_msg_parts = [
        f"tool: {tool_result.tool_name}",
        f"payload: {json.dumps(trimmed, ensure_ascii=False)}",
    ]
    if tool_result.composer_hint:
        user_msg_parts.append(f"hint: {tool_result.composer_hint}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *_FEW_SHOT,
        {"role": "user", "content": "\n".join(user_msg_parts)},
    ]


class AnswerComposer:
    def compose(
        self,
        tool_result: ToolResult,
        session: AgentSession,
        timeout: float = 10.0,
    ) -> str:
        """非流式：阻塞到 LLM 返回完整答案。"""
        if tool_result.narrative_override is not None:
            return tool_result.narrative_override
        if not tool_result.needs_composer:
            return ""

        client = get_client()
        response = client.chat.completions.create(
            model=get_model_id(),
            messages=_build_messages(tool_result),
            temperature=0.4,
            timeout=timeout,
        )
        return (response.choices[0].message.content or "").strip()

    def compose_stream(
        self,
        tool_result: ToolResult,
        session: AgentSession,
        timeout: float = 30.0,
    ) -> Iterator[str]:
        """流式：逐 chunk yield 文本片段。

        - 当 tool 已经给了 narrative_override，仍然 yield 一次整段，
          调用方无需特判，CLI / SSE 拼接逻辑一致。
        - LLM 流失败时不抛异常：已经 yield 的内容保留，再 yield 一段
          兜底提示。这样前端不会卡死在半截答案上。
        """
        if tool_result.narrative_override is not None:
            yield tool_result.narrative_override
            return
        if not tool_result.needs_composer:
            return

        client = get_client()
        try:
            stream = client.chat.completions.create(
                model=get_model_id(),
                messages=_build_messages(tool_result),
                temperature=0.4,
                timeout=timeout,
                stream=True,
            )
            emitted_any = False
            for chunk in stream:
                # OpenAI SDK：chunk.choices[0].delta.content；豆包 Ark 一致。
                try:
                    delta = chunk.choices[0].delta
                except (AttributeError, IndexError):
                    continue
                piece = getattr(delta, "content", None)
                if piece:
                    emitted_any = True
                    yield piece
            if not emitted_any:
                # 模型一个 token 都没吐（理论极少见），给个保底
                yield "（暂未生成内容，请换种说法再试一次）"
        except Exception as exc:  # noqa: BLE001 - 流式中任何异常都要兜住
            logger.exception("Streaming compose failed mid-flight")
            yield f"\n[生成中断：{type(exc).__name__}]"
