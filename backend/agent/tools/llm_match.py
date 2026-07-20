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
    """Call the LLM once and return valid zero-based candidate indices.

    The result preserves order and uniqueness. Uncertainty, unavailability, or parsing
    failure returns an empty list. Rich candidate fields improve disambiguation.
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
    except Exception as exc:  # noqa: BLE001 - disambiguation failures degrade safely
        logger.warning("llm_match failed; falling back to rule-based clarification: %r", exc)
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
    """Return the zero-based index of the single most likely referenced candidate.

    ``None`` means uncertain or unavailable; callers should clarify and must not treat
    it as candidate zero.
    """
    picked = _llm_pick(query, candidates, timeout)
    return picked[0] if picked else None


def llm_pick_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    k: int = 2,
    timeout: float = 5.0,
) -> list[int]:
    """Return up to ``k`` referenced candidate indices in stable unique order.

    An empty list means uncertain or unavailable and should trigger clarification.
    """
    if k <= 0:
        return []
    return _llm_pick(query, candidates, timeout)[:k]


def _extract_indices(response: Any) -> list[int]:
    """Extract integer indices from a function-call response, or return an empty list."""
    try:
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        if not tool_calls:
            return []
        arguments = tool_calls[0].function.arguments
        data = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        raw = data.get("indices")
        if raw is None:
            single = data.get("index")  # Accept the occasional singular field.
            return [int(single)] if single not in (None, 0) else []
        if isinstance(raw, (int, float)):
            return [int(raw)]
        return [int(x) for x in raw]
    except (AttributeError, IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse the llm_match response: %r", exc)
        return []
