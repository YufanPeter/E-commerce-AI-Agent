from __future__ import annotations

"""RecommendTool：把现成的 SearchService 包装成一个 Tool。

职责（窄而清晰）：
    - 调 SearchService.search() 拿 hits
    - 把 hits 和 parsed query 塞进 working_memory，给后续 refine 用
    - 返回 ToolResult，让 composer 生成自然语言话术

不做的事：
    - 不做 fallback 降级（hits=[] 时如实返回，让 composer 用合适的话术）
    - 不直接生成话术（那是 composer 的事，便于流式 / 个性化注入）
"""

from typing import Any, Callable

from agent.session import AgentSession
from agent.tools.base import ToolResult
from search.query_decomposer import SubRequest, decompose_query
from search.search_service import ProductHit, SearchResult, SearchService


# 默认 top-k；composer 不需要更多，前端卡片场景 5 已经够。
DEFAULT_TOP_K = 5

# 多需求分组时，每个子需求保留的商品数。比单需求少，是因为多个品类
# 叠加后总量会很大；每类取 top-3 既能覆盖主流选择，又不让卡片列表过长。
PER_GROUP_TOP_K = 3


class RecommendTool:
    name: str = "recommend"

    def __init__(
        self,
        search_service: SearchService | None = None,
        decomposer: Callable[[str], list[SubRequest]] | None = None,
    ) -> None:
        # 懒加载：首次 run() 时才实例化 SearchService。
        # SearchService 构造会加载 embedding 模型 + 打开 Chroma（~10s），
        # 放在 __init__ 会让 CLI 启动时卡住几十秒无法响应。
        self._service = search_service
        # 多需求拆解器：默认用 LLM 版 decompose_query；测试可注入 stub 避免网络调用。
        self._decompose = decomposer or decompose_query

    def _get_service(self) -> SearchService:
        if self._service is None:
            from search.search_service import get_search_service
            self._service = get_search_service()
        return self._service

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        top_k = int(slots.get("top_k", DEFAULT_TOP_K))
        # base_parsed 由 RefineTool 注入：表示这是一次"细化"，需把本轮解析叠加到
        # 上一轮结构化意图上（见 SearchService.search / ParsedQuery.merge_base）。
        base = slots.get("base_parsed")

        # 细化（refine）路径绝不拆解：细化是承接上一轮的【单一意图】，
        # 把它拆成多需求会打乱 merge_base 的上下文叠加。直接走单路检索。
        if base is not None:
            return self._run_single(query, session, top_k=top_k, base=base)

        # 首轮推荐：尝试把"一句话多需求"拆成多个子需求。
        # decompose 内部已做容错——失败时返回单元素列表，等价于不拆。
        subs = self._decompose(query)
        if len(subs) <= 1:
            return self._run_single(query, session, top_k=top_k, base=None)
        return self._run_multi(query, subs, session)

    # ------------------------------------------------------------------
    # 单需求路径（原逻辑，保持不变）
    # ------------------------------------------------------------------
    def _run_single(
        self,
        query: str,
        session: AgentSession,
        top_k: int,
        base: Any,
    ) -> ToolResult:
        result = self._get_service().search(query, top_k_products=top_k, base=base)

        # 工作记忆（WorkingMemory 契约）：refine/compare/detail 后续会读这俩字段。
        # 存的是【结构化】ParsedQuery（dict），下一轮才能做无损约束叠加。
        session.remember_search(
            result.parsed.to_dict(),
            [{"product_id": h.product_id, "title": h.title} for h in result.hits],
        )

        products = [_to_product_card(h) for h in result.hits]

        summary = {
            "hit_count": len(result.hits),
            "needs_clarification": result.parsed.needs_clarification,
            "category": result.parsed.category,
            "max_price": result.parsed.max_price,
        }

        payload = {
            "query": query,
            "products": products,
            "summary": summary,
            # debug 块给开发者排查用，前端可忽略
            "debug": {
                "parsed": result.parsed.to_dict(),
                "raw_chunk_count": result.raw_chunk_count,
                "filtered_chunk_count": result.filtered_chunk_count,
                "hits_full": [h.to_dict() for h in result.hits],
            },
        }

        # 给 composer 一点 hint，让它对零命中、需澄清做不同应答
        if result.parsed.needs_clarification:
            hint = "检索系统认为 query 过于模糊，请引导用户补充关键信息（品类、预算、用途）。"
        elif not result.hits:
            hint = "本次未命中任何商品，请坦诚告知用户并给出可能放宽的方向建议。"
        else:
            hint = f"已为用户找到 {len(result.hits)} 款商品，请按推荐理由+对比维度的方式简明介绍。"

        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=hint,
        )

    # ------------------------------------------------------------------
    # 多需求路径（fan-out + 分组合并）
    # ------------------------------------------------------------------
    def _run_multi(
        self,
        query: str,
        subs: list[SubRequest],
        session: AgentSession,
    ) -> ToolResult:
        service = self._get_service()

        # 对每个子需求各跑一次完整检索管线，结果按子需求分组。
        # 跨组去重：同一商品若被多个子需求命中，只归入【第一个】命中的组，
        # 避免前端重复展示，也让"防晒/衣服"边界清晰。
        groups: list[dict[str, Any]] = []
        flat_products: list[dict[str, Any]] = []
        flat_hit_refs: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        first_parsed: dict[str, Any] | None = None

        for sub in subs:
            result: SearchResult = service.search(sub.query, top_k_products=PER_GROUP_TOP_K)
            if first_parsed is None:
                first_parsed = result.parsed.to_dict()

            group_products: list[dict[str, Any]] = []
            for h in result.hits:
                if h.product_id in seen_ids:
                    continue
                seen_ids.add(h.product_id)
                card = _to_product_card(h)
                group_products.append(card)
                flat_products.append(card)
                flat_hit_refs.append({"product_id": h.product_id, "title": h.title})

            groups.append(
                {
                    "label": sub.label,
                    "query": sub.query,
                    "products": group_products,
                }
            )

        # 工作记忆：多需求场景下，last_hits 存合并后的全部命中（供 compare/detail 回指）；
        # last_parsed_query 退而存第一个子需求的解析——多组结果对 refine 本就语义模糊，
        # 这里给一个合理基线即可，不追求无损。
        session.remember_search(first_parsed or {}, flat_hit_refs)

        non_empty_groups = [g for g in groups if g["products"]]
        summary = {
            "hit_count": len(flat_products),
            "group_count": len(non_empty_groups),
            "groups": [
                {"label": g["label"], "hit_count": len(g["products"])} for g in groups
            ],
            "needs_clarification": False,
        }

        payload = {
            "query": query,
            "products": flat_products,   # 扁平列表，保持与单需求一致的旧契约
            "groups": groups,            # 新增：分组结构，前端可选用渲染分区
            "summary": summary,
            "debug": {
                "multi_intent": True,
                "sub_requests": [s.to_dict() for s in subs],
            },
        }

        labels = "、".join(g["label"] for g in non_empty_groups) or "多个品类"
        if not flat_products:
            hint = "本次多个需求都未命中商品，请坦诚告知用户并给出放宽建议。"
        else:
            hint = (
                f"用户一次提了多个需求（{labels}），已分别检索。"
                "请【按需求分组】依次介绍，每组先点出需求名再说推荐理由，"
                "让用户清楚每类各挑了什么、为什么适合。"
            )

        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=hint,
        )


def _to_product_card(h: ProductHit) -> dict[str, Any]:
    """给前端的精简商品卡片：只保留渲染需要的字段。"""
    return {
        "product_id": h.product_id,
        "title": h.title,
        "brand": h.brand,
        "category": h.category,
        "sub_category": h.sub_category,
        "price": h.base_price,
    }
