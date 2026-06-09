from __future__ import annotations

"""LLM 商品消歧：把用户口语映射到候选商品库里的某一款（或某几款）。

为什么需要它：
    规则匹配（reference.resolve_by_name）只会拿「商品 title/brand 里的词」去
    query 里找子串。当用户说「华为耳机」，候选「华为 FreeBud（真无线耳机）」和
    「华为 Pura 90（智能手机）」都含「华为」→ 两个都命中 → 又反问一遍，死循环。
    问题本质：规则看不懂「耳机 ≈ 子品类·真无线耳机」这种语义。

设计：
    把候选的 title/brand/category/sub_category 结构化喂给 LLM，让它按语义挑出
    用户指向的商品编号（可能 1 个，compare 场景可能 2 个）。挑不出（真歧义/都
    不沾边）就返回空，交回规则层继续反问。
    全程「增强而非依赖」：LLM 不可用 / 超时 / 返回异常都安全降级为空结果，
    绝不让一次消歧失败把整条链路带崩。

对外接口：
    - llm_pick_candidate(query, candidates)  → int | None      挑唯一一款
    - llm_pick_candidates(query, candidates, k) → list[int]     挑最多 k 款（保序去重）
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是电商导购里的「商品指向判定器」。下面给你一份候选商品清单（每项含编号、标题、品牌、品类、子品类）。请判断用户这句话指向其中哪一款或哪几款。

判定要点：
1. 用户常用口语/品类词，而非完整商品名。要把它对应到候选的「子品类/品类/品牌」上，例如：
   - 「耳机」「蓝牙耳机」→ 子品类是「真无线耳机」的那款；
   - 「手机」→ 子品类「智能手机」；「平板」→「平板电脑」；
   - 「面膜」「防晒」→ 对应美妆子品类；只报品牌名就按品牌匹配。
2. 用户可能一次点名多款（如「华为和小米这两款」「第一个和第三个」）——把所有命中的编号都返回。
3. 能明确锁定就返回对应编号（数组）。如果完全无法区分、或这句跟所有候选都不沾边，返回空数组（表示无法确定，需要继续追问）。
只调用 pick_products 函数返回结果，不要输出多余文字。"""


_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "pick_products",
        "description": "返回用户指向的候选商品编号（从 1 开始）；无法确定时返回空数组。",
        "parameters": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "命中的候选编号列表（从 1 开始），按用户语序。无法确定时为空数组。",
                },
                "reason": {
                    "type": "string",
                    "description": "一句话依据，便于排查（如「耳机对应真无线耳机=候选2」）。",
                },
            },
            "required": ["indices"],
        },
    },
}


def _format_candidates(candidates: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        title = str(cand.get("title", "")).strip() or f"第{i}款"
        brand = str(cand.get("brand", "")).strip()
        category = str(cand.get("category", "")).strip()
        sub_category = str(cand.get("sub_category", "")).strip()
        parts = [f"{i}. 标题：{title}"]
        if brand:
            parts.append(f"品牌：{brand}")
        if category:
            parts.append(f"品类：{category}")
        if sub_category:
            parts.append(f"子品类：{sub_category}")
        lines.append("｜".join(parts))
    return "\n".join(lines)


def _llm_pick(
    query: str,
    candidates: list[dict[str, Any]],
    timeout: float,
) -> list[int]:
    """底层：调一次 LLM，返回命中的 0-based 索引列表（保序去重、越界已过滤）。

    无法确定 / LLM 不可用 / 解析失败 都返回空列表。candidates 每项建议含
    title/brand/category/sub_category，信息越全消歧越准。
    """
    text = (query or "").strip()
    if not text or len(candidates) < 2:
        return []

    try:
        from llm.client import get_client, get_model_id

        client = get_client()
        user_content = (
            f"用户这句话：{text}\n\n候选商品：\n{_format_candidates(candidates)}\n\n"
            f"请判断用户指向哪一款或哪几款（无法确定就返回空数组）。"
        )
        response = client.chat.completions.create(
            model=get_model_id(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "pick_products"}},
            temperature=0.0,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 消歧是增强项，任何失败都安全降级
        logger.warning("llm_match 调用失败，降级为规则反问：%r", exc)
        return []

    result: list[int] = []
    for raw in _extract_indices(response):
        idx = raw - 1  # 1-based → 0-based
        if 0 <= idx < len(candidates) and idx not in result:
            result.append(idx)
    return result


def llm_pick_candidate(
    query: str,
    candidates: list[dict[str, Any]],
    timeout: float = 5.0,
) -> int | None:
    """让 LLM 在候选里挑用户最可能指向的唯一一款，返回 0-based 索引。

    返回 None 表示「无法确定」或「LLM 不可用」——调用方应据此继续走反问，
    绝不能把 None 当成「第 0 款」。
    """
    picked = _llm_pick(query, candidates, timeout)
    return picked[0] if picked else None


def llm_pick_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    k: int = 2,
    timeout: float = 5.0,
) -> list[int]:
    """让 LLM 在候选里挑用户指向的最多 k 款，返回 0-based 索引列表（保序去重）。

    返回空列表表示「无法确定」或「LLM 不可用」，调用方据此继续反问。
    """
    if k <= 0:
        return []
    return _llm_pick(query, candidates, timeout)[:k]


def _extract_indices(response: Any) -> list[int]:
    """从 function-calling 响应里取出 indices 整数列表。解析失败返回空列表。"""
    try:
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        if not tool_calls:
            return []
        arguments = tool_calls[0].function.arguments
        data = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        raw = data.get("indices")
        if raw is None:
            single = data.get("index")  # 兼容模型偶发只回单个 index
            return [int(single)] if single not in (None, 0) else []
        if isinstance(raw, (int, float)):
            return [int(raw)]
        return [int(x) for x in raw]
    except (AttributeError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("llm_match 响应解析失败：%r", exc)
        return []
