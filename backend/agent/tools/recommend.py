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

import logging
import os
from typing import Any, Callable

from agent.session import AgentSession
from agent.tools.base import ToolResult
from search.query_decomposer import SubRequest, decompose_query
from search.search_service import ProductHit, SearchResult, SearchService


logger = logging.getLogger(__name__)

# 默认 top-k；composer 不需要更多，前端卡片场景 5 已经够。
DEFAULT_TOP_K = 5

# 图搜重排融合权重：最终分 = 文本相关性 * α + 视觉相似度 * β。
# 文本（VLM 抽取的 query 走 RAG）保证"类目/属性对得上"，视觉保证"长得像"，
# 两者互补。默认偏重文本（语义意图更贴合电商"找同类"），视觉做次级排序。
_VISUAL_TEXT_WEIGHT = float(os.getenv("VISUAL_TEXT_WEIGHT", "0.6"))
_VISUAL_IMAGE_WEIGHT = float(os.getenv("VISUAL_IMAGE_WEIGHT", "0.4"))

# 图搜召回池：先用文本多召回一些，再用视觉相似度重排取 top_k，给重排留空间。
_VISUAL_POOL_SIZE = int(os.getenv("VISUAL_POOL_SIZE", "12"))

# 多需求分组时，每个子需求保留的商品数。比单需求少，是因为多个品类
# 叠加后总量会很大；每类取 top-3 既能覆盖主流选择，又不让卡片列表过长。
PER_GROUP_TOP_K = 3


class RecommendTool:
    name: str = "recommend"

    def __init__(
        self,
        search_service: SearchService | None = None,
        decomposer: Callable[[str], list[SubRequest]] | None = None,
        product_store: Any | None = None,
    ) -> None:
        # 懒加载：首次 run() 时才实例化 SearchService。
        # SearchService 构造会加载 embedding 模型 + 打开 Chroma（~10s），
        # 放在 __init__ 会让 CLI 启动时卡住几十秒无法响应。
        self._service = search_service
        # 多需求拆解器：默认用 LLM 版 decompose_query；测试可注入 stub 避免网络调用。
        self._decompose = decomposer or decompose_query
        # 商品事实库：用于取价格区间，给卡片/composer 生成「¥X 起」的价格展示。
        self._products = product_store

    def _get_service(self) -> SearchService:
        if self._service is None:
            from search.search_service import get_search_service
            self._service = get_search_service()
        return self._service

    def _get_products(self) -> Any:
        if self._products is None:
            from store.product_store import ProductStore
            self._products = ProductStore()
        return self._products

    def _attach_price_displays(self, cards: list[dict[str, Any]]) -> None:
        """给商品卡补上预格式化的价格展示「price_display」（多规格→¥X 起）。

        composer 拿着裸数字 base_price 容易把多规格商品报成单一价（甚至说成某个
        偏高的 SKU 价）；这里用真实价格区间统一生成「起价」字符串，让价格语义由
        代码确定。取价失败时退回单价显示，不中断推荐。"""
        if not cards:
            return
        from store.product_store import price_display

        ids = [c["product_id"] for c in cards if c.get("product_id")]
        try:
            candidates = self._get_products().get_products_by_ids(ids)
            by_id = {c.product_id: c for c in candidates}
        except Exception as exc:  # noqa: BLE001 - 价格展示是增强项，失败不应中断推荐
            logger.warning("取价格区间失败，价格展示退回单价：%r", exc)
            by_id = {}
        for card in cards:
            cand = by_id.get(card.get("product_id"))
            if cand is not None:
                card["price_display"] = price_display(cand.price_range)
            else:
                card["price_display"] = f"¥{card.get('price', 0):g}"

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
    # 图搜路径（拍照找货）：VLM 已抽好的 query 走文本 RAG 召回，再用视觉相似度重排
    # ------------------------------------------------------------------
    def run_image(
        self,
        query: str,
        image: str,
        session: AgentSession,
        top_k: int = DEFAULT_TOP_K,
    ) -> ToolResult:
        """图搜：``query`` 是 VLM 从图里抽出的检索词，``image`` 是原图（URL/base64）。

        流程：文本 RAG 多召回一池候选 → 用图片向量对候选算视觉相似度 →
        融合（文本相关性 + 视觉相似度）重排 → 取 top_k → 复用商品卡片契约。
        视觉索引缺失或编码失败时，自动退化为纯文本顺序（不报错）。
        """
        pool = max(top_k, _VISUAL_POOL_SIZE)
        result = self._get_service().search(query, top_k_products=pool)

        hits = self._visual_rerank(result.hits, image, top_k)

        session.remember_search(
            result.parsed.to_dict(),
            [{"product_id": h.product_id, "title": h.title} for h in hits],
        )

        products = [_to_product_card(h) for h in hits]
        self._attach_price_displays(products)
        payload = {
            "query": query,
            "products": products,
            "summary": {
                "hit_count": len(hits),
                "needs_clarification": False,
                "category": result.parsed.category,
                "source": "visual_search",
            },
            "debug": {
                "parsed": result.parsed.to_dict(),
                "pool_size": len(result.hits),
                "extracted_query": query,
            },
        }
        if not hits:
            hint = (
                "用户上传了一张商品图，但库里没有找到相似商品。"
                "请坦诚告知，并建议换张更清晰的图或用文字描述。"
            )
        else:
            hint = (
                f"用户上传了一张商品图，我识别为「{query}」并找到 {len(hits)} 款相似商品。"
                "请说明这是【根据图片】找到的相似/同类商品，再简明介绍推荐理由。"
            )
        return ToolResult(tool_name=self.name, payload=payload, composer_hint=hint)

    def _visual_rerank(
        self, hits: list[ProductHit], image: str, top_k: int
    ) -> list[ProductHit]:
        """用图片视觉相似度对文本召回的候选重排，取 top_k。

        融合分 = 归一化文本分 * α + 视觉余弦 * β。视觉信号拿不到（索引缺失/
        编码失败/该商品无图向量）时，β 部分记 0，等价于退化到纯文本排序。"""
        if not hits:
            return []

        # 1) 文本分 min-max 归一化到 [0,1]，让两路信号同量纲再融合。
        scores = [h.score for h in hits]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0
        text_norm = {h.product_id: (h.score - lo) / span for h in hits}

        # 2) 视觉相似度（拿不到则整体退化为纯文本）。
        visual: dict[str, float] = {}
        try:
            from llm.vision import embed_image
            from search.visual_index import get_visual_index

            query_vec = embed_image(image)
            visual = get_visual_index().score_many(
                query_vec, [h.product_id for h in hits]
            )
        except Exception as exc:  # noqa: BLE001 - 视觉重排是增强项，失败不应中断图搜
            logger.warning("视觉重排失败，退化为纯文本排序：%r", exc)

        def _fused(h: ProductHit) -> float:
            t = text_norm.get(h.product_id, 0.0)
            v = visual.get(h.product_id)
            if v is None:
                return t  # 无视觉信号 → 只看文本
            return _VISUAL_TEXT_WEIGHT * t + _VISUAL_IMAGE_WEIGHT * v

        ranked = sorted(hits, key=_fused, reverse=True)
        return ranked[:top_k]


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
        self._attach_price_displays(products)

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

        # 价格展示：flat_products 与各 group 的 card 是同一批 dict 对象，补一次即可全覆盖。
        self._attach_price_displays(flat_products)

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
