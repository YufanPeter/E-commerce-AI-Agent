from __future__ import annotations

"""AnswerComposer：把 ToolResult 转成自然语言回答。

提供三种入口：
    - compose():        一次性返回完整 narrative（/chat 非流式、后端测试）
    - compose_events(): **流式结构化事件**——开场白逐字、每款商品解说一到就发、
                        追问逐条，最后给一份完整 narrative（SSE 主路径）
    - compose_stream(): 兼容旧接口，只 yield 一次最终 narrative 字符串

为什么从"流式 JSON"改成"行协议"：
    之前让模型吐 JSON，流式时只能攒到最后一个 `}` 才能解析，前端看到的其实是
    一整块、而不是"先说一句开场、再一张张弹卡片"。这里让模型按【行】输出：

        开场｜为你推荐了 5 款 500 元以内的真无线耳机
        商品｜p_digital_001｜音质细腻、主动降噪给力，通勤地铁里也清净
        商品｜p_digital_002｜……
        追问｜推荐一些更平价的选择
        追问｜对比一下前两款

    每行自带类型，解析器可以边收边发：开场白按 token 推送，商品行一结束就推送
    该商品解说，追问逐条推送。最终再把所有行折叠成与旧版完全一致的 JSON
    ``{"opening","items","followup"}``，旧客户端与 /chat 接口零改动。

为什么独立成模块（而不是塞进 tool 里）：
    - tool 关心"做了什么"（payload），composer 关心"怎么说出来"。
    - 流式输出只需改 composer，tool 完全不动。
"""

import hashlib
import json
import logging
from collections.abc import Iterator
from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from llm.cache import TTLCache
from llm.client import close_stream, get_client, get_model_id


logger = logging.getLogger(__name__)


# 行协议分隔符：全角竖线几乎不会出现在中文解说正文里，比冒号/逗号可靠。
SEP = "｜"
_OPENING_TAG = "开场"
_ITEM_TAG = "商品"
_FOLLOWUP_TAG = "追问"
_GROUP_TAG = "分组"
_SUMMARY_TAG = "总结"


SYSTEM_PROMPT = """你是一位友好、专业的电商导购助手，说话像门店里真正懂行的导购，而不是念商品参数的机器人。

输出格式（必须严格遵守，这是机器解析的行协议）：
- 逐行输出，每行一条记录，字段之间用全角竖线「｜」分隔，不要输出任何其他文字、不要 Markdown、不要 JSON。
- 第 1 行必须是开场白：`开场｜<一句话>`，用"为你XXX了"的格式（如"开场｜为你推荐了几款适合敏感肌的护肤品"）。
- 有 2 款及以上商品时，把它们按真实差异分成 1-3 组，每组商品行之前先输出一行 `分组｜<小标题>`。小标题 4-14 字，说清这组的共同点（如"分组｜轻薄便携，通勤首选"），不要写品牌和价格。只有 1 款时不用分组。
- 每款商品一行：`商品｜<productId>｜<解说>`。productId 必须原样使用 payload.hits 里的 productId，禁止自造序号；每款商品一行专属解说，不能多款共用。解说写 1-2 句、约 40 个中文字符（30-60 字），先点核心卖点再补适用场景或人群，内容要具体，**绝不能包含商品名、品牌名、型号或价格**。
- 有 2 款及以上商品时，在追问之前输出一行总结：`总结｜<内容>`。先说明不同需求分别选哪款（这里可以用品牌或简称点名），最后用一个问句问用户最在意什么，共 2-3 句。
- 最后输出 3-5 行追问：`追问｜<用户视角的一句话>`。每条都是用户亲口对导购说的话，点击后会直接发送。
    - 正确："追问｜推荐其他版型的阿迪达斯运动裤"、"追问｜找加绒的秋冬款"
    - 错误（禁止）："需要其他版型吗？"、"要不要看看其他品牌？" —— 这是助手在问用户
    - 用祈使/请求口吻（推荐/找/看看/对比/帮我…），禁止"要不要""需要吗""想要吗"等问句；结合当前推荐写具体可执行的下一步。

内容要求：
- 中文，简洁自然，整体控制在 400 字以内。
- 只基于 payload 中的真实商品信息说话，绝不编造任何不存在的字段或商品。
- 卡片本身已展示价格；除非开场白里做总体价格梯度说明，否则解说里不要重复价格。
- 多规格商品的价格是区间（hits[].price_display 形如「¥450 – ¥900」，price_range 给 min/max），不要把它说成单一价。用户给了预算时 hits[].budget 里有预算内规格数（skus_within_budget），可以在开场白里点一句，例如"有两款全系都在预算内，另一款只有基础版不超预算"。
- 介绍每款时落到使用场景和人群（通勤、学习、送礼…），不要写保养建议、注意事项或长段参数。
- 适度点出关键差异帮用户决策（价格梯度、核心卖点、适合谁）。
- 若 payload.hits 为空：开场白坦诚告知未找到并给出具体放宽建议（提高预算到 X、放宽品牌等），不输出商品行，追问给放宽方向。
- 若 payload 里有 groups（用户一次提了多个需求，如"衣服和防晒"）：开场白里分别点明各需求，商品行按组的顺序依次输出，先输出一行 `分组｜<需求名>` 再跟该组的商品行。
- 若 hint 提到图片搜索，开场白要说明这是根据图片找到的相似/同类商品。"""


PRODUCT_DETAIL_SYSTEM_PROMPT = """你是一位专业的单品导购问答助手。用户已经指向了某一款商品，你的任务是基于 payload.product 和 payload.evidence 回答用户追问。

严格规则：
- 只输出自然中文，不要输出 JSON。
- 回答控制在 3-5 句，直接回应用户问点。
- 必须基于 payload.evidence 和 payload.product，不要编造没有证据的信息。
- 如果 payload.focus_aspect 是 reviews 或 negative_reviews，必须严格分成 3 个独立段落，每段单独一行，不要额外加开场或结尾。
- 评价类回答要简洁但有信息量：每段写 1-2 句，每段不超过 70 个中文字符；优点和缺点各总结 2-3 个核心点，不要写成大段流水账。
    ✨ 总结：概括整体口碑，可补一句适合谁/整体倾向。
    🌟 优点：总结最明显的 2-3 个好处；没有足够证据就说“正向信息不多”。
    🔍 缺点：总结最明显的 2-3 个顾虑；没有足够证据就说“未见集中差评”。
- 如果 payload.focus_aspect 是 general 或 performance，用户是在了解产品本身：只输出 1 个自然段，不要分点、不要换行、不要 emoji，控制在 90 个中文字符以内。
- 通用/性能类回答要像导购简短介绍：包含产品定位、1-2 个核心卖点，以及适合人群或购买前注意点；禁止写成长段参数介绍。
- 注意区分用户意图：general/performance 是了解产品本身，用单段简介；reviews/negative_reviews 是看评价口碑，用“优点/缺点”三段。
- 如果用户问评价/口碑，优先总结 user_review 证据里的共性，兼顾正负两面。
- 如果用户问差评/缺点，仍保持“总结/优点/缺点”三段式，但缺点段必须优先说明负面证据；如果负面证据不足，要明确说目前证据有限。
- 如果用户问敏感肌/安全性，不要做医疗承诺，只能说“从现有评价/FAQ 看”。
- 评价类回答不要逐条引用用户原话；只有非评价类追问才可适度引用一两条证据来源，例如“有用户提到…”。
- 证据不足时要坦诚，不要硬夸。"""


# Few-shot 示例：用真实对话演示"导购口吻 + 行协议"远比文字规则有效。
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
            "开场｜为你推荐了 5 款 5000 元以内的高性价比数码\n"
            "分组｜通勤听歌，降噪优先\n"
            "商品｜p_digital_001｜音质细腻、主动降噪给力，通勤地铁里也清净，长时间佩戴也不胀耳\n"
            "分组｜学习追剧，性价比高\n"
            "商品｜p_digital_002｜性能够用又不贵，刷剧上网课都流畅，学生党日常学习娱乐都合适\n"
            "商品｜p_digital_003｜配置均衡、屏幕素质不错，日常追剧办公和轻度游戏都能轻松应对\n"
            "分组｜大屏办公，生产力强\n"
            "商品｜p_digital_004｜M4 芯片性能强劲，剪辑修图都跟得上，适合追求生产力的进阶用户\n"
            "商品｜p_digital_005｜12.6 寸大屏视野开阔，搭配键盘办公高效，居家移动办公都得心应手\n"
            "总结｜只想要安静通勤，华为耳机最省心；上课追剧选 vivo 或小米平板，花费最少；要剪辑办公就看 iPad Air 或 MatePad。你更看重便携还是屏幕大？\n"
            "追问｜推荐一些更平价的选择\n"
            "追问｜推荐其他品牌的商品\n"
            "追问｜对比一下刚才推荐的两款"
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
            "开场｜抱歉，300元以内的真无线耳机暂时没有合适的货，预算放宽到 500 左右选择会多很多\n"
            "追问｜预算放宽到500以内再推荐\n"
            "追问｜不限品牌，推荐平价真无线耳机\n"
            "追问｜推荐有线耳机替代"
        ),
    },
    {
        "role": "user",
        "content": (
            'tool: recommend\n'
            'payload: {"query": "阿迪达斯运动长裤", "parsed": {"brand_include": "阿迪达斯", "sub_category": "运动长裤"}, '
            '"hits": [{"productId": "p_sport_001", "title": "阿迪达斯 经典三条纹收口长裤", "brand": "阿迪达斯", "sub_category": "运动长裤"}]}'
        ),
    },
    {
        "role": "assistant",
        "content": (
            "开场｜为你推荐了几款阿迪达斯运动长裤\n"
            "商品｜p_sport_001｜面料亲肤、版型利落，日常通勤逛街和轻运动都好搭，穿着舒适不挑身材\n"
            "追问｜推荐其他版型的阿迪达斯运动裤\n"
            "追问｜看看更多配色可选的\n"
            "追问｜找加绒的秋冬款\n"
            "追问｜推荐其他品牌的运动长裤"
        ),
    },
]


# payload 里塞 LLM 不需要的字段（如 evidence chunk）只会浪费 token。
# 这里裁剪只保留对话术有用的精简字段。
_HIT_KEEP_KEYS = ("title", "brand", "category", "sub_category", "price_display", "price", "base_price", "score", "price_range", "budget")


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
    if payload.get("product"):
        trimmed["product"] = payload.get("product")
    if payload.get("focus_aspect"):
        trimmed["focus_aspect"] = payload.get("focus_aspect")
    if payload.get("evidence"):
        trimmed["evidence"] = payload.get("evidence")
    contextual = payload.get("contextual_search")
    if isinstance(contextual, dict):
        trimmed["contextual_search"] = {
            k: contextual.get(k)
            for k in (
                "mode", "target_query", "target_terms", "target_sub_categories",
                "anchor_query", "anchor_sub_category", "exclude_sub_categories", "relation",
            )
            if contextual.get(k)
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


def _is_plain_text_tool(tool_result: ToolResult) -> bool:
    """product_detail 输出自然段落而非行协议，按纯文本逐 token 流式。"""
    return tool_result.tool_name == "product_detail"


def _build_messages(tool_result: ToolResult) -> list[dict[str, str]]:
    trimmed = _trim_payload_for_llm(tool_result.payload)
    user_msg_parts = [
        f"tool: {tool_result.tool_name}",
        f"payload: {json.dumps(trimmed, ensure_ascii=False)}",
    ]
    if tool_result.composer_hint:
        user_msg_parts.append(f"hint: {tool_result.composer_hint}")
    plain = _is_plain_text_tool(tool_result)
    system_prompt = PRODUCT_DETAIL_SYSTEM_PROMPT if plain else SYSTEM_PROMPT
    few_shot = [] if plain else _FEW_SHOT
    return [
        {"role": "system", "content": system_prompt},
        *few_shot,
        {"role": "user", "content": "\n".join(user_msg_parts)},
    ]


# ---------------------------------------------------------------------------
# 行协议解析
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> str | None:
    """Extract the first balanced JSON object, tolerating model preface/trailing text."""
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:index + 1]
                try:
                    json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return candidate
    return None


def _normalize_json_response(text: str) -> str:
    stripped = (text or "").strip()
    return _extract_json_object(stripped) or stripped


def _split_line(line: str) -> list[str]:
    """按全角竖线切分；兼容模型偶尔写成半角 ``|``。"""
    raw = line.strip().strip("`")
    if SEP in raw:
        return [p.strip() for p in raw.split(SEP)]
    if "|" in raw:
        return [p.strip() for p in raw.split("|")]
    return [raw]


def _strip_list_prefix(text: str) -> str:
    """去掉模型偶尔加的 "- "、"1. " 之类前缀。"""
    t = text.lstrip()
    for prefix in ("- ", "• ", "* "):
        if t.startswith(prefix):
            return t[len(prefix):].strip()
    i = 0
    while i < len(t) and t[i].isdigit():
        i += 1
    if 0 < i < 3 and i < len(t) and t[i] in ".、)）":
        return t[i + 1:].strip()
    return t


def _parse_protocol_line(line: str) -> dict[str, Any] | None:
    """把一行解析成 {"part": ..., ...}；非协议行返回 None。"""
    text = _strip_list_prefix(line)
    if not text:
        return None
    parts = _split_line(text)
    if len(parts) < 2:
        return None
    tag = parts[0].strip("「」[]【】 ").strip()
    if tag == _OPENING_TAG:
        return {"part": "opening", "text": SEP.join(parts[1:]).strip()}
    if tag == _ITEM_TAG:
        if len(parts) < 3:
            return None
        return {"part": "item", "productId": parts[1].strip("「」\"' "), "description": SEP.join(parts[2:]).strip()}
    if tag == _FOLLOWUP_TAG:
        return {"part": "followup", "text": SEP.join(parts[1:]).strip()}
    if tag == _GROUP_TAG:
        return {"part": "group", "label": SEP.join(parts[1:]).strip()}
    if tag == _SUMMARY_TAG:
        return {"part": "summary", "text": SEP.join(parts[1:]).strip()}
    return None


class _ProtocolStreamParser:
    """增量解析行协议：开场白逐 delta 推送，其他行在换行时整条推送。

    用法：每收到一个 LLM chunk 调 ``feed(piece)``，得到 0..n 个事件；
    流结束调 ``finish()`` 冲出最后一行。``structured`` 累计所有已解析字段。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._opening_streaming = False   # 当前行已识别为开场白，正在逐字下发
        self._opening_emitted = 0         # 开场白已下发的字符数
        self.opening = ""
        self.items: list[dict[str, str]] = []
        self.followup: list[str] = []
        self.groups: list[str] = []
        self.summary = ""
        self._current_group = ""          # 后续商品行归属的分区
        self.protocol_lines = 0           # 成功解析的协议行数（0 → 模型没按协议输出）
        self.raw = ""

    def feed(self, piece: str) -> list[dict[str, Any]]:
        self.raw += piece
        self._buf += piece
        events: list[dict[str, Any]] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            events.extend(self._complete_line(line))
        events.extend(self._stream_opening_prefix())
        return events

    def finish(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self._buf.strip():
            events.extend(self._complete_line(self._buf))
        self._buf = ""
        return events

    # -- 内部 ------------------------------------------------------------

    def _stream_opening_prefix(self) -> list[dict[str, Any]]:
        """行未结束但已能确认是开场白时，把分隔符后的新字符作为 delta 发出去。"""
        if not self._buf or self.opening:
            return []
        head = _strip_list_prefix(self._buf)
        if not self._opening_streaming:
            marker = None
            for candidate in (f"{_OPENING_TAG}{SEP}", f"{_OPENING_TAG}|"):
                if head.startswith(candidate):
                    marker = candidate
                    break
            if marker is None:
                return []
            self._opening_streaming = True
            self._opening_emitted = 0
        marker_len = len(_OPENING_TAG) + 1
        text = head[marker_len:]
        if len(text) <= self._opening_emitted:
            return []
        delta = text[self._opening_emitted:]
        self._opening_emitted = len(text)
        return [{"part": "opening", "delta": delta}]

    def _complete_line(self, line: str) -> list[dict[str, Any]]:
        parsed = _parse_protocol_line(line)
        was_streaming = self._opening_streaming
        self._opening_streaming = False
        if parsed is None:
            return []
        self.protocol_lines += 1
        part = parsed["part"]
        if part == "opening":
            if self.opening:
                # 模型重复给开场白：并入第一条，不再重复推送
                return []
            self.opening = parsed["text"]
            if was_streaming:
                remainder = self.opening[self._opening_emitted:]
                self._opening_emitted = len(self.opening)
                out: list[dict[str, Any]] = []
                if remainder:
                    out.append({"part": "opening", "delta": remainder})
                out.append({"part": "opening_end", "text": self.opening})
                return out
            return [
                {"part": "opening", "delta": self.opening},
                {"part": "opening_end", "text": self.opening},
            ]
        if part == "item":
            item = {"productId": parsed["productId"], "description": parsed["description"]}
            if self._current_group:
                item["group"] = self._current_group
            if any(i["productId"] == item["productId"] for i in self.items):
                return []
            self.items.append(item)
            return [{"part": "item", "index": len(self.items) - 1, **item}]
        if part == "followup":
            text = parsed["text"]
            if not text or text in self.followup:
                return []
            self.followup.append(text)
            return [{"part": "followup", "index": len(self.followup) - 1, "text": text}]
        if part == "group":
            label = parsed["label"]
            if not label or label == self._current_group:
                return []
            self._current_group = label
            self.groups.append(label)
            return [{"part": "group", "label": label}]
        if part == "summary":
            if self.summary or not parsed["text"]:
                return []
            self.summary = parsed["text"]
            return [{"part": "summary", "text": self.summary}]
        return []

    def to_structured(self) -> dict[str, Any]:
        data: dict[str, Any] = {"opening": self.opening, "items": self.items, "followup": self.followup}
        if self.summary:
            data["summary"] = self.summary
        return data

    def to_json(self) -> str:
        return json.dumps(
            self.to_structured(),
            ensure_ascii=False,
        )


def _protocol_text_to_json(text: str) -> str | None:
    """把一段完整的行协议文本折叠成 JSON 字符串；不是行协议则返回 None。"""
    parser = _ProtocolStreamParser()
    parser.feed(text if text.endswith("\n") else text + "\n")
    parser.finish()
    if parser.protocol_lines == 0:
        return None
    return parser.to_json()


def _structured_from_json(text: str) -> dict[str, Any] | None:
    """模型若无视行协议直接输出了 JSON，仍把它解析成结构化字典。"""
    candidate = _extract_json_object((text or "").strip())
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "opening" not in data:
        return None
    items = []
    for raw in data.get("items") or []:
        if isinstance(raw, dict) and raw.get("productId"):
            item = {"productId": str(raw["productId"]), "description": str(raw.get("description") or "")}
            if raw.get("group"):
                item["group"] = str(raw["group"])
            items.append(item)
    followup = [str(x) for x in (data.get("followup") or data.get("questions") or []) if str(x).strip()]
    structured: dict[str, Any] = {"opening": str(data.get("opening") or ""), "items": items, "followup": followup}
    if data.get("summary"):
        structured["summary"] = str(data["summary"])
    return structured


def _events_from_structured(data: dict[str, Any]) -> list[dict[str, Any]]:
    """把一份完整结构化结果展开成与流式一致的事件序列（缓存命中 / JSON 兜底时用）。"""
    events: list[dict[str, Any]] = []
    opening = data.get("opening") or ""
    if opening:
        events.append({"part": "opening", "delta": opening})
        events.append({"part": "opening_end", "text": opening})
    current_group = ""
    for index, item in enumerate(data.get("items") or []):
        group = item.get("group") or ""
        if group and group != current_group:
            current_group = group
            events.append({"part": "group", "label": group})
        events.append({"part": "item", "index": index, **item})
    if data.get("summary"):
        events.append({"part": "summary", "text": data["summary"]})
    for index, text in enumerate(data.get("followup") or []):
        events.append({"part": "followup", "index": index, "text": text})
    return events


def _fallback_structured(tool_result: ToolResult) -> dict[str, Any]:
    payload = tool_result.payload or {}
    hits = payload.get("products") or payload.get("hits") or []
    items = []
    for hit in hits[:5]:
        product_id = hit.get("product_id") or hit.get("productId") or ""
        title = hit.get("title") or "这款商品"
        if not product_id:
            continue
        items.append(
            {
                "productId": product_id,
                "description": f"{title}整体匹配你的需求，可以先点开详情看看。",
            }
        )

    if items:
        opening = f"为你找到 {len(items)} 款可以优先看的商品"
        followup = ["推荐一些更平价的选择", "看看更多同类商品", "对比一下刚才推荐的两款"]
    else:
        opening = "暂时没有生成完整说明，可以换个说法再试一次"
        followup = ["预算放宽一些再推荐", "换个品牌看看", "重新帮我推荐"]
    return {"opening": opening, "items": items, "followup": followup}


def _fallback_json_response(tool_result: ToolResult) -> str:
    payload = tool_result.payload or {}
    if _is_plain_text_tool(tool_result):
        product = payload.get("product") or {}
        title = product.get("title") or "这款商品"
        evidence_note = "现有证据有限，"
        return f"{evidence_note}{title}可以先参考商品描述、问答和用户评价再判断；如果你想看评价、差评或使用建议，可以继续具体问我。"
    return json.dumps(_fallback_structured(tool_result), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 话术缓存：同一批商品 + 同一 hint → 同一段话术。
# ---------------------------------------------------------------------------

_NARRATIVE_CACHE: TTLCache[dict[str, Any]] = TTLCache(maxsize=256)


def _cache_key(tool_result: ToolResult) -> str:
    trimmed = _trim_payload_for_llm(tool_result.payload)
    raw = json.dumps(
        {"tool": tool_result.tool_name, "hint": tool_result.composer_hint, "payload": trimmed},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def clear_narrative_cache() -> None:
    _NARRATIVE_CACHE.clear()


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

class AnswerComposer:
    def compose(
        self,
        tool_result: ToolResult,
        session: AgentSession,
        timeout: float = 10.0,
    ) -> str:
        """非流式：阻塞到 LLM 返回完整答案（行协议会折叠成 JSON）。"""
        if tool_result.narrative_override is not None:
            return tool_result.narrative_override
        if not tool_result.needs_composer:
            return ""

        if not _is_plain_text_tool(tool_result):
            cached = _NARRATIVE_CACHE.get(_cache_key(tool_result))
            if cached is not None:
                return json.dumps(cached, ensure_ascii=False)

        client = get_client()
        response = client.chat.completions.create(
            model=get_model_id(role="composer"),
            messages=_build_messages(tool_result),
            temperature=0.4,
            timeout=timeout,
        )
        text = response.choices[0].message.content or ""
        if _is_plain_text_tool(tool_result):
            return text.strip()
        as_json = _protocol_text_to_json(text)
        if as_json is not None:
            try:
                _NARRATIVE_CACHE.set(_cache_key(tool_result), json.loads(as_json))
            except json.JSONDecodeError:
                pass
            return as_json
        return _normalize_json_response(text)

    def compose_events(
        self,
        tool_result: ToolResult,
        session: AgentSession,
        timeout: float = 30.0,
    ) -> Iterator[dict[str, Any]]:
        """流式结构化事件。yield 的 dict 形如：

            {"type": "narrative", "data": {"part": "opening", "delta": "为你"}}
            {"type": "narrative", "data": {"part": "opening_end", "text": "..."}}
            {"type": "narrative", "data": {"part": "group", "label": "..."}}
            {"type": "narrative", "data": {"part": "item", "index": 0, "productId": "...", "description": "...", "group": "..."}}
            {"type": "narrative", "data": {"part": "summary", "text": "..."}}
            {"type": "narrative", "data": {"part": "followup", "index": 0, "text": "..."}}
            {"type": "token", "data": "一段纯文本"}            # product_detail / override
            {"type": "final", "data": "<完整 narrative 字符串>"}  # 永远是最后一个

        LLM 流失败不抛异常：已经发出的事件保留，再补一份兜底 final。
        """
        if tool_result.narrative_override is not None:
            yield {"type": "token", "data": tool_result.narrative_override}
            yield {"type": "final", "data": tool_result.narrative_override}
            return
        if not tool_result.needs_composer:
            yield {"type": "final", "data": ""}
            return

        if _is_plain_text_tool(tool_result):
            yield from self._plain_text_events(tool_result, timeout)
            return

        key = _cache_key(tool_result)
        cached = _NARRATIVE_CACHE.get(key)
        if cached is not None:
            logger.info("composer cache hit (tool=%s)", tool_result.tool_name)
            for ev in _events_from_structured(cached):
                yield {"type": "narrative", "data": ev}
            yield {"type": "final", "data": json.dumps(cached, ensure_ascii=False)}
            return

        parser = _ProtocolStreamParser()
        stream = None
        try:
            stream = get_client().chat.completions.create(
                model=get_model_id(role="composer"),
                messages=_build_messages(tool_result),
                temperature=0.4,
                timeout=timeout,
                stream=True,
            )
            for chunk in stream:
                piece = _chunk_text(chunk)
                if not piece:
                    continue
                for ev in parser.feed(piece):
                    yield {"type": "narrative", "data": ev}
            for ev in parser.finish():
                yield {"type": "narrative", "data": ev}
        except Exception:  # noqa: BLE001 - 流式中任何异常都要兜住
            logger.exception("Streaming compose failed mid-flight")
            if parser.protocol_lines == 0:
                data = _fallback_structured(tool_result)
                for ev in _events_from_structured(data):
                    yield {"type": "narrative", "data": ev}
                yield {"type": "final", "data": json.dumps(data, ensure_ascii=False)}
                return
            # 已经吐了一部分协议行：保留已有内容，补默认追问即可
            if not parser.followup:
                for text in _fallback_structured(tool_result)["followup"]:
                    parser.followup.append(text)
                    yield {"type": "narrative", "data": {"part": "followup", "index": len(parser.followup) - 1, "text": text}}
            yield {"type": "final", "data": parser.to_json()}
            return
        finally:
            # 客户端断连时这里收到 GeneratorExit：关掉上游流，模型不再继续生成。
            close_stream(stream)

        if parser.protocol_lines > 0:
            _NARRATIVE_CACHE.set(key, parser.to_structured())
            yield {"type": "final", "data": parser.to_json()}
            return

        # 模型没按行协议输出：可能是 JSON（兼容旧 prompt 风格），也可能是纯文本。
        structured = _structured_from_json(parser.raw)
        if structured is not None:
            _NARRATIVE_CACHE.set(key, structured)
            for ev in _events_from_structured(structured):
                yield {"type": "narrative", "data": ev}
            yield {"type": "final", "data": json.dumps(structured, ensure_ascii=False)}
            return
        text = parser.raw.strip()
        if text:
            yield {"type": "token", "data": text}
            yield {"type": "final", "data": text}
            return
        # 模型一个 token 都没吐（理论极少见），给个保底
        data = _fallback_structured(tool_result)
        for ev in _events_from_structured(data):
            yield {"type": "narrative", "data": ev}
        yield {"type": "final", "data": json.dumps(data, ensure_ascii=False)}

    def _plain_text_events(self, tool_result: ToolResult, timeout: float) -> Iterator[dict[str, Any]]:
        """product_detail：自然段落，真正逐 token 流式。"""
        pieces: list[str] = []
        stream = None
        try:
            stream = get_client().chat.completions.create(
                model=get_model_id(role="composer"),
                messages=_build_messages(tool_result),
                temperature=0.4,
                timeout=timeout,
                stream=True,
            )
            for chunk in stream:
                piece = _chunk_text(chunk)
                if not piece:
                    continue
                pieces.append(piece)
                yield {"type": "token", "data": piece}
        except Exception:  # noqa: BLE001
            logger.exception("Streaming compose failed mid-flight (plain text)")
            if not pieces:
                text = _fallback_json_response(tool_result)
                yield {"type": "token", "data": text}
                yield {"type": "final", "data": text}
                return
        finally:
            close_stream(stream)
        text = "".join(pieces).strip()
        if not text:
            text = _fallback_json_response(tool_result)
            yield {"type": "token", "data": text}
        yield {"type": "final", "data": text}

    def compose_stream(
        self,
        tool_result: ToolResult,
        session: AgentSession,
        timeout: float = 30.0,
    ) -> Iterator[str]:
        """兼容入口：只 yield 一次最终 narrative（CLI / 旧调用方）。"""
        for ev in self.compose_events(tool_result, session, timeout=timeout):
            if ev["type"] == "final":
                if ev["data"]:
                    yield ev["data"]
                return


def _chunk_text(chunk: Any) -> str | None:
    """OpenAI SDK：chunk.choices[0].delta.content；DeepSeek / 百炼兼容接口一致。"""
    try:
        delta = chunk.choices[0].delta
    except (AttributeError, IndexError):
        return None
    return getattr(delta, "content", None)
