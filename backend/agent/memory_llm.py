from __future__ import annotations

"""LLM 记忆抽取：补确定性规则覆盖不到的口语（「我这人怕麻烦」「家里有两个娃」）。

模型的输出不直接生效，全部经过代码校验：

- 必须给出用户原话（quote），且原话真的出现在这句话里，防止模型编造；
- 必须是用户自己的长期情况，一次性需求由模型标记 durable=false 后丢弃；
- 品牌必须在商品库里；
- 会挡掉商品的记忆（不买某品牌、忌讳某成分）和健康相关内容只进待确认，用户点了才生效。
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from llm.client import get_client, get_model_id
from search.llm_parser import _loads_arguments
from search.query_understanding import load_taxonomy


logger = logging.getLogger(__name__)

# 不值得花一次 LLM 的句子：没有任何自述信号。
_DISCLOSURE_CUES = (
    "我", "平时", "一般", "习惯", "以后", "总是", "一直", "家里", "怕", "讨厌", "喜欢",
    "不吃", "不喝", "过敏", "皮肤", "肤质",
)
_THIRD_PARTY_HINTS = ("给我妈", "给我爸", "女朋友", "男朋友", "老婆", "老公", "送人", "送给")
# 这一次的购物动作：「我想看看华为手机」不是长期偏好，模型有时会判错，先在代码里挡掉。
_ONE_OFF_HINTS = ("这次", "今天", "看看", "试试", "暂时", "本次", "我想", "我要", "帮我", "给我")

ALLOWED_KINDS = ("brand_like", "brand_avoid", "style", "budget", "skin_type", "shoe_size", "clothing_size", "avoid_term", "household")
# 这些会把商品直接挡掉，模型说的不算数，要用户确认。
_CONFIRM_REQUIRED = {"brand_avoid", "avoid_term"}

_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_memories",
        "description": "记录用户这句话里透露的、关于他自己的长期购物情况。",
        "parameters": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": list(ALLOWED_KINDS)},
                            "value": {"type": "string", "description": "精炼后的值，如 油皮、42码、小米、轻量、有两个小孩、酒精。预算写成上限数字，如 3000。"},
                            "quote": {"type": "string", "description": "用户原话里支撑这条记忆的连续片段，必须逐字摘录。"},
                            "durable": {"type": "boolean", "description": "是用户长期的情况为 true；只是这一次的购物需求为 false。"},
                        },
                        "required": ["kind", "value", "quote", "durable"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["memories"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = """你负责判断用户这句话有没有透露【他本人长期】的购物情况。必须调用 record_memories，没有就传空数组。

记：肤质、尺码、长期喜欢或不买的品牌、长期风格偏好（轻量、性价比、质感）、长期预算习惯、家庭情况（家里有小孩）、过敏和忌口。
不记：这一次的需求（"推荐通勤的双肩包""预算 500 的耳机"）、替别人买的情况、对商品的提问、对这款商品的一次性评价。

例：
"我这人特别怕麻烦，东西好用就行" → 不确定算什么偏好，传空数组。
"家里两个娃，零食得买小包装的" → household：有两个小孩。
"用惯了小米，换别的不习惯" → brand_like：小米。
"推荐一款轻便的电脑" → 空数组，这是一次性需求。"""


@dataclass(frozen=True)
class ExtractedMemory:
    kind: str
    value: str
    quote: str
    needs_confirm: bool


def might_disclose(text: str) -> bool:
    """确定性预判，绝大多数纯购物句子不打 LLM。"""
    if not text or len(text) < 4 or any(hint in text for hint in _THIRD_PARTY_HINTS):
        return False
    if any(hint in text for hint in _ONE_OFF_HINTS):
        return False
    return any(cue in text for cue in _DISCLOSURE_CUES)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def validate_memories(raw: list[dict[str, Any]], text: str) -> list[ExtractedMemory]:
    """把模型输出过一遍规则。放在独立函数里，不依赖网络就能测。"""
    taxonomy = load_taxonomy()
    brands = set(taxonomy.get("brands", []))
    for variants in (taxonomy.get("brand_expand") or {}).values():
        brands.update(variants)
    out: list[ExtractedMemory] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict) or item.get("durable") is not True:
            continue
        kind = item.get("kind")
        value = str(item.get("value") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if kind not in ALLOWED_KINDS or not value or not quote:
            continue
        if _compact(quote) not in _compact(text):
            logger.info("memory_llm: 丢弃找不到原话的记忆 %r", item)
            continue
        if kind in {"brand_like", "brand_avoid"} and value not in brands and value not in text:
            continue
        if kind == "budget" and not re.fullmatch(r"\d{2,6}", value):
            continue
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        out.append(ExtractedMemory(kind=kind, value=value, quote=quote, needs_confirm=kind in _CONFIRM_REQUIRED))
    return out


def extract_with_llm(text: str, timeout: float = 5.0) -> list[ExtractedMemory]:
    """失败一律返回空列表：记忆抽取不能影响对话。"""
    try:
        response = get_client().chat.completions.create(
            model=get_model_id(role="fast"),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            tools=[_TOOL],
            tool_choice={"type": "function", "function": {"name": "record_memories"}},
            temperature=0.0,
            timeout=timeout,
        )
        calls = response.choices[0].message.tool_calls
        if not calls:
            return []
        args = _loads_arguments(calls[0].function.arguments)
        return validate_memories(args.get("memories") or [], text)
    except Exception:  # noqa: BLE001
        logger.warning("memory_llm 抽取失败", exc_info=True)
        return []
