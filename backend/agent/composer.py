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


SYSTEM_PROMPT = """你是一位友好、专业的电商导购助手。

风格要求：
- 中文回答，简洁自然，避免冗长。
- 直接基于提供的 payload 中的真实商品信息说话，不要编造任何不存在的字段。
- 如果有多个商品，可适度对比关键差异（价格、卖点、人群）。
- 若 payload.hits 为空，请坦诚告知未找到，并建议用户放宽条件（如提高预算、放宽品牌）。
- 不要复述商品 JSON 字段名，用自然语言介绍。
- 控制在 200 字以内。"""


# payload 里塞 LLM 不需要的字段（如 evidence chunk）只会浪费 token。
# 这里裁剪只保留对话术有用的精简字段。
_HIT_KEEP_KEYS = ("title", "brand", "category", "sub_category", "price", "base_price", "score")


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
    trimmed["hits"] = [
        {k: h.get(k) for k in _HIT_KEEP_KEYS if k in h}
        for h in hits
    ]
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
