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


def focus_index_in(hits: list[dict[str, Any]], session: Any) -> int | None:
    """当前聚焦商品（``last_focus_product_id``）在 hits 里的下标；没有则 None。

    「上一个 / 下一个」这类相对指代以它为锚点。"""
    focus_id = session.get("last_focus_product_id") if session is not None else None
    if not focus_id:
        return None
    for i, hit in enumerate(hits):
        if str(hit.get("product_id")) == str(focus_id):
            return i
    return None


def enrich_candidates(
    candidates: list[dict[str, Any]],
    store: Any,
) -> list[dict[str, Any]]:
    """给候选补齐 brand / category / sub_category / options（SKU 规格值），三个工具共用。

    options 形如 {"颜色": ["黑色", "白色"], "尺码": ["41", "42"]}，只保留取值多于一个
    的维度——这些才是「黑色那个」「42 码的」能用来区分的属性；规则匹配
    （resolve_by_name）和 LLM 消歧（llm_match）都会读它。
    富化失败（取详情异常）不影响主流程——仍可只凭 title 消歧。"""
    enriched: list[dict[str, Any]] = []
    for cand in candidates:
        item = dict(cand)
        if "options" not in item and store is not None:
            try:
                detail = store.get_product_detail(item["product_id"])
            except Exception:  # noqa: BLE001
                detail = None
            if detail is not None:
                item.setdefault("title", detail.title)
                item["brand"] = detail.brand
                item["category"] = detail.category
                item["sub_category"] = detail.sub_category
                item["options"] = _sku_options(getattr(detail, "skus", None) or [])
        enriched.append(item)
    return enriched


def _sku_options(skus: list[Any]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for sku in skus:
        props = getattr(sku, "properties", None)
        if props is None and isinstance(sku, dict):
            props = sku.get("properties") or sku.get("options")
        if not isinstance(props, dict):
            continue
        for key, value in props.items():
            text = str(value).strip()
            if not text:
                continue
            bucket = values.setdefault(str(key), [])
            if text not in bucket:
                bucket.append(text)
    # 单值维度也要留着：一款手机只有「星芒黑」一个颜色，但正是它把这款和别的区分开；
    # 「是否在候选之间有区分度」由 resolve_by_name 在候选集合层面判断。
    return values


def resolve_one(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    use_llm: bool = True,
    focus_index: int | None = None,
) -> int | None:
    """在候选里定位用户指向的【唯一一款】，返回 0-based 索引；定不了返回 None。

    分层：序号唯一（含「上一个 / 下一个」相对指代，以 focus_index 为锚）→
    名称 / 规格值唯一 →（歧义时）LLM 消歧。candidates 每项建议带
    title/brand/category/sub_category/options，LLM 消歧才有依据。
    """
    if not candidates:
        return None

    indices = resolve_indices(query, len(candidates), focus_index=focus_index)
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
    focus_index: int | None = None,
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
    for i in resolve_indices(query, len(candidates), focus_index=focus_index):
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
