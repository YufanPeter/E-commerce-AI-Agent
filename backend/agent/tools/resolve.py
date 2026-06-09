from __future__ import annotations

"""统一的「商品指代解析」分层 resolver（detail / compare / cart 共用）。

为什么要它：
    detail/compare/cart 三个工具各自手写过一遍「把用户这句话在一组候选里挑出
    商品」的逻辑，且只有 detail 接了 LLM 语义消歧。本模块把这段抽出来，让三个
    工具共享同一套「确定性优先、语义兜底」的分层策略，从而：
      ① 所有工具/品类都获得 LLM 消歧能力（"华为耳机"不再死循环）；
      ② 删掉三处重复的挑选代码。

分层原则（关键）：
    确定性的捷径走规则（0 延迟、0 成本、100% 准）；只有规则产生「真歧义」
    （0 个或 ≥2 个名称命中）时才落 LLM。绝不把"第一个"这类高频序号也丢给 LLM。
      ① 序号命中（"第一个/前两个/最后一个"）  → resolve_indices
      ② 名称唯一命中                          → resolve_by_name
      ③ 名称 0 个或 ≥2 个（歧义）             → llm_pick_candidate(s) 语义兜底
      ④ LLM 也定不了                          → 返回候选，交工具反问

LLM 始终「增强而非依赖」：不可用/超时/异常一律降级回规则，绝不把链路带崩。
"""

import logging
from typing import Any

from agent.tools.llm_match import llm_pick_candidate, llm_pick_candidates
from agent.tools.reference import resolve_by_name, resolve_indices

logger = logging.getLogger(__name__)


def resolve_one(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    use_llm: bool = True,
) -> int | None:
    """在候选里定位用户指向的【唯一一款】，返回 0-based 索引；定不了返回 None。

    分层：序号唯一 → 名称唯一 →（歧义时）LLM 消歧。candidates 每项建议带
    title/brand/category/sub_category，LLM 消歧才有依据。
    """
    if not candidates:
        return None

    indices = resolve_indices(query, len(candidates))
    if indices:
        i = indices[0]
        if 0 <= i < len(candidates):
            return i

    named = resolve_by_name(query, candidates)
    if len(named) == 1:
        return named[0]

    # 名称 0 个或 ≥2 个 → 歧义，交给 LLM 按语义挑唯一一款。
    if use_llm:
        picked = llm_pick_candidate(query, candidates)
        if picked is not None and 0 <= picked < len(candidates):
            return picked

    return None


def resolve_many(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    k: int = 2,
    use_llm: bool = True,
) -> list[int]:
    """在候选里定位用户指向的【最多 k 款】，返回 0-based 索引列表（保序去重）。

    分层：序号（"第一个和第三个"，确定可信）优先；不足 k 时看名称命中——
    名称命中**不超过缺口**就采纳（确定），否则名称里有品牌歧义（如"华为"命中
    华为耳机+华为手机两款），交 LLM 按语义仲裁补齐。LLM 不可用时退回名称命中。
    用于 compare 这类需要 2 款的场景。定不到足量返回已有的（可能为空）。
    """
    if not candidates or k <= 0:
        return []

    # ① 序号永远可信，先收。
    picked: list[int] = []
    for i in resolve_indices(query, len(candidates)):
        if 0 <= i < len(candidates) and i not in picked:
            picked.append(i)
    if len(picked) >= k:
        return picked[:k]

    name_hits = [i for i in resolve_by_name(query, candidates) if i not in picked]
    gap = k - len(picked)

    # ② 名称命中没溢出缺口 → 确定，直接采纳。
    if len(name_hits) <= gap:
        picked.extend(name_hits)
        if len(picked) >= k or not use_llm:
            return picked[:k]
        # 仍不足 k → LLM 兜底补齐。
        for i in llm_pick_candidates(query, candidates, k=k):
            if i not in picked:
                picked.append(i)
        return picked[:k]

    # ③ 名称命中溢出缺口（品牌歧义）→ 交 LLM 按语义仲裁；不可用才退回名称命中。
    if use_llm:
        llm_hits = [i for i in llm_pick_candidates(query, candidates, k=k) if i not in picked]
        if llm_hits:
            picked.extend(llm_hits)
            return picked[:k]
    picked.extend(name_hits)
    return picked[:k]
