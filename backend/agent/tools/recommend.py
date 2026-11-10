from __future__ import annotations

"""Tool wrapper around the existing ``SearchService``.

Responsibilities:
    - Call ``SearchService.search`` for product hits.
    - Store hits and parsed intent in working memory for later refinement.
    - Return ``ToolResult`` for natural-language composition.

It deliberately does not hide empty results or generate response copy directly.
"""

import logging
import os
from dataclasses import replace
from typing import Any, Callable

from agent.contextual_search import ContextualSearchPlan, detect_contextual_plan
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.evidence import card_evidence
from i18n import en as english
from i18n import is_english
from catalog_config import PROFILE
from search.query_decomposer import SubRequest, decompose_query
from search.query_understanding import ParsedQuery
from search.search_service import ProductHit, SearchResult, SearchService, confirmed_tier


logger = logging.getLogger(__name__)

# Default result count for the composer and product-card UI.
DEFAULT_TOP_K = 5

# Visual-search fusion: text relevance * alpha + visual similarity * beta. Text carries
# category and attribute intent; visual similarity acts as a secondary ranking signal.
_VISUAL_TEXT_WEIGHT = float(os.getenv("VISUAL_TEXT_WEIGHT", "0.6"))
_VISUAL_IMAGE_WEIGHT = float(os.getenv("VISUAL_IMAGE_WEIGHT", "0.4"))

# Retrieve a wider text pool before visual reranking.
_VISUAL_POOL_SIZE = int(os.getenv("VISUAL_POOL_SIZE", "12"))

# Per-demand result count, kept smaller to bound the combined card list.
PER_GROUP_TOP_K = 3

# Cap each brand during broad browsing so one dominant brand does not fill the page.
_MAX_PER_BRAND = 2

# Cap each subcategory only when browsing an entire category, preserving focused queries.
_MAX_PER_SUBCATEGORY = 2

# Candidate-pool multiplier used before brand and subcategory diversity filtering.
_DIVERSITY_POOL_MULTIPLIER = 5

# 预算匹配加成权重：相关性归一到 [0,1] 后，预算内规格占比最多再加这么多。
_BUDGET_FIT_WEIGHT = float(os.getenv("BUDGET_FIT_WEIGHT", "0.15"))

# 品牌别名归一：库里同一品牌存在中英文/简称多种写法（Apple 苹果 vs 苹果、Nike vs 耐克），
# 做品牌配额时若不归一，别名会被当成不同品牌而绕过配额（典型："只有苹果"）。
# key 为「小写去空格」后的写法，value 为规范品牌键。
_BRAND_ALIASES = {
    "apple苹果": "apple",
    "苹果": "apple",
    "apple": "apple",
    "nike": "nike",
    "耐克": "nike",
    "thenorthface": "thenorthface",
    "北面": "thenorthface",
}


def _canonical_brand(brand: str) -> str:
    """Normalize brand aliases to a canonical diversity key."""
    norm = (brand or "").strip().lower().replace(" ", "")
    return _BRAND_ALIASES.get(norm, norm)


def _diversify(
    hits: list[ProductHit],
    top_k: int,
    caps: list[tuple[Callable[[ProductHit], str], int]],
) -> list[ProductHit]:
    """Apply key-based concentration caps while preserving relevance order.

    Greedily accept a hit only when every cap allows it, preserving rejected hits as
    overflow. If the primary set is short, fill it from overflow in original order.
    This is intended for broad browsing, not explicit brand requests.
    """
    primary: list[ProductHit] = []
    overflow: list[ProductHit] = []
    counters: list[dict[str, int]] = [{} for _ in caps]
    for h in hits:
        keys = [key_fn(h) for key_fn, _ in caps]
        if all(counters[i].get(keys[i], 0) < caps[i][1] for i in range(len(caps))):
            primary.append(h)
            for i, k in enumerate(keys):
                counters[i][k] = counters[i].get(k, 0) + 1
            if len(primary) >= top_k:
                return primary
        else:
            overflow.append(h)
    if len(primary) < top_k:
        primary.extend(overflow[: top_k - len(primary)])
    return primary[:top_k]


class RecommendTool:
    name: str = "recommend"

    def __init__(
        self,
        search_service: SearchService | None = None,
        decomposer: Callable[[str], list[SubRequest]] | None = None,
        product_store: Any | None = None,
    ) -> None:
        # Initialize SearchService lazily because it loads the embedding model and Chroma.
        self._service = search_service
        # Tests may inject a decomposer stub to avoid network calls.
        self._decompose = decomposer or decompose_query
        # Product facts provide price ranges for deterministic display copy.
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

    @staticmethod
    def _search(
        service: SearchService,
        query: str,
        session: AgentSession,
        **kwargs: Any,
    ) -> SearchResult:
        profile = session.user_profile or None
        if profile:
            try:
                return service.search(query, user_profile=profile, **kwargs)
            except TypeError:
                # Some tests inject tiny stubs that predate user_profile.
                pass
        return service.search(query, **kwargs)

    def _attach_price_displays(
        self,
        cards: list[dict[str, Any]],
        parsed: ParsedQuery | None = None,
    ) -> None:
        """给商品卡补上价格区间与预算匹配信息。

        - ``price_display``：单价「¥450」或区间「¥450 – ¥900」，由真实 SKU 区间生成，
          避免 composer 把多规格商品报成某个偏高/偏低的单价。
        - ``price_range``：{min, max, sku_count}。
        - ``budget``（用户说了预算时）：{max_price, min_price, skus_within_budget,
          cheapest_sku_id, cheapest_price}——卡片据此提示「预算内 N 个规格」，
          详情页默认选中预算内最便宜的规格。
        取价失败时退回单价显示，不中断推荐。"""
        if not cards:
            return
        from store.product_store import price_display

        ids = [c["product_id"] for c in cards if c.get("product_id")]
        try:
            candidates = self._get_products().get_products_by_ids(ids)
            by_id = {c.product_id: c for c in candidates}
        except Exception as exc:  # noqa: BLE001 - price display must not stop retrieval
            logger.warning("Failed to load the price range; falling back to the unit price: %r", exc)
            by_id = {}
        budget_max = getattr(parsed, "max_price", None) if parsed is not None else None
        budget_min = getattr(parsed, "min_price", None) if parsed is not None else None
        for card in cards:
            cand = by_id.get(card.get("product_id"))
            if cand is None:
                card["price_display"] = f"¥{card.get('price', 0):g}"
                continue
            rng = cand.price_range
            card["price_display"] = price_display(rng)
            card["price_range"] = {
                "min": rng.min_price,
                "max": rng.max_price,
                "sku_count": rng.sku_count,
            }
            if (PROFILE.capabilities.reference_prices and rng.is_reference) or (budget_max is None and budget_min is None):
                continue
            card["budget"] = self._budget_fit(card["product_id"], budget_min, budget_max)

    def _budget_fit(
        self,
        product_id: str,
        budget_min: float | None,
        budget_max: float | None,
    ) -> dict[str, Any]:
        """统计预算内的 SKU：数量 + 最便宜那个（给详情页 / 加购做默认规格）。"""
        try:
            skus = self._get_products().get_skus(product_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("取 SKU 失败，预算匹配信息跳过：%r", exc)
            skus = []
        within = [
            sku for sku in skus
            if (budget_max is None or sku.price <= budget_max)
            and (budget_min is None or sku.price >= budget_min)
        ]
        cheapest = min(within, key=lambda s: s.price) if within else None
        return {
            "max_price": budget_max,
            "min_price": budget_min,
            "skus_within_budget": len(within),
            "sku_count": len(skus),
            "cheapest_sku_id": cheapest.sku_id if cheapest else None,
            "cheapest_price": cheapest.price if cheapest else None,
        }

    @staticmethod
    def _apply_budget_fit_bonus(hits: list[ProductHit], parsed: ParsedQuery | None) -> list[ProductHit]:
        """有预算时给「预算内规格占比高」的商品一点排序加成。

        只靠 min_price ≤ 预算召回，会让"只有一个最低配刚好卡线"的商品和"全系都在预算内"
        的商品并列。这里把分数 min-max 归一后加 ``_BUDGET_FIT_WEIGHT * 占比``，
        相关性仍是主项，占比只做次级排序。"""
        if not hits or parsed is None:
            return hits
        hi_budget = getattr(parsed, "max_price", None)
        lo_budget = getattr(parsed, "min_price", None)
        if hi_budget is None and lo_budget is None:
            return hits
        scores = [float(getattr(h, "score", 0.0) or 0.0) for h in hits]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0

        def _fraction(h: ProductHit) -> float:
            base = float(getattr(h, "base_price", 0.0) or 0.0)
            lo_p = getattr(h, "min_price", None)
            hi_p = getattr(h, "max_price", None)
            lo_p = base if lo_p is None else float(lo_p)
            hi_p = base if hi_p is None else float(hi_p)
            if hi_p <= lo_p:
                inside = (hi_budget is None or lo_p <= hi_budget) and (lo_budget is None or lo_p >= lo_budget)
                return 1.0 if inside else 0.0
            # 没有逐 SKU 数据时，用价格区间与预算区间的重叠比例近似
            upper = min(hi_p, hi_budget) if hi_budget is not None else hi_p
            lower = max(lo_p, lo_budget) if lo_budget is not None else lo_p
            return max(0.0, min(1.0, (upper - lower) / (hi_p - lo_p)))

        def _key(h: ProductHit) -> tuple[int, float]:
            fit = (float(getattr(h, "score", 0.0) or 0.0) - lo) / span + _BUDGET_FIT_WEIGHT * _fraction(h)
            return confirmed_tier(h, parsed), fit

        return sorted(hits, key=_key, reverse=True)

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        top_k = int(slots.get("top_k", DEFAULT_TOP_K))
        # RefineTool injects ``base_parsed`` so current constraints merge with prior intent.
        base = slots.get("base_parsed")

        more_label = slots.get("more_label")
        if more_label is not None:
            return self._run_more(query, more_label, session, top_k, base)

        # complement / pivot：本轮目标已经变成“搭配上衣/看看运动鞋”等新目标，
        # 上一轮品类只能当语境，不能进入检索词或硬过滤。
        context_base = base if isinstance(base, ParsedQuery) else self._base_from_session(session)
        plan = detect_contextual_plan(query, context_base)
        if plan is not None:
            return self._run_contextual(query, plan, session, top_k=top_k)

        # Never decompose refinement; it extends one prior intent through ``merge_base``.
        if base is not None:
            return self._run_single(query, session, top_k=top_k, base=base)

        # Decompose multi-need first-turn requests; failure returns one unchanged request.
        subs = self._decompose(query)
        if len(subs) <= 1:
            return self._run_single(query, session, top_k=top_k, base=None)
        return self._run_multi(query, subs, session)

    @staticmethod
    def _base_from_session(session: AgentSession) -> ParsedQuery | None:
        last = session.recall_parsed()
        if not isinstance(last, dict):
            return None
        try:
            return ParsedQuery.from_dict(last)
        except TypeError:
            return None

    # ------------------------------------------------------------------
    # Visual search: retrieve with VLM terms, then rerank by image similarity.
    # ------------------------------------------------------------------
    def run_image(
        self,
        query: str,
        image: str,
        session: AgentSession,
        top_k: int = DEFAULT_TOP_K,
        image_embedding: Any | None = None,
        parsed: ParsedQuery | None = None,
    ) -> ToolResult:
        """Search by image using VLM terms and the source image URL or base64 data.

        流程：文本 RAG 多召回一池候选 → 用图片向量对候选算视觉相似度 →
        融合（文本相关性 + 视觉相似度）重排 → 取 top_k → 复用商品卡片契约。
        视觉索引缺失或编码失败时，自动退化为纯文本顺序（不报错）。

        ``image_embedding``：orchestrator 在看图（VLM）的同时已经后台算好的
        ``(provider, vector)``，或一个 ``concurrent.futures.Future``；传了就不再
        重复调用 embedding 接口。
        """
        pool = max(top_k, _VISUAL_POOL_SIZE)
        search_kwargs: dict[str, Any] = {"top_k_products": pool}
        if parsed is not None:
            # 调用方已给出结构化意图（图搜：由 VLM 短语确定性构造）：跳过 LLM query 解析
            search_kwargs["parsed"] = parsed
        result = self._search(
            self._get_service(),
            query,
            session,
            **search_kwargs,
        )

        hits = result.hits[:top_k] if not PROFILE.capabilities.image_similarity else self._visual_rerank(
            result.hits, image, top_k, image_embedding=image_embedding
        )

        session.remember_search(
            result.parsed.to_dict(),
            [{"product_id": h.product_id, "title": h.title} for h in hits],
        )

        products = [_card_with_evidence(h, query) for h in hits]
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
        if is_english():
            hint = english.VISUAL_HINT.format(query=query) if hits else english.VISUAL_EMPTY_HINT
        return ToolResult(tool_name=self.name, payload=payload, composer_hint=hint)

    def _visual_rerank(
        self,
        hits: list[ProductHit],
        image: str,
        top_k: int,
        image_embedding: Any | None = None,
    ) -> list[ProductHit]:
        """Rerank text candidates by visual similarity and return the top results.

        The fused score combines normalized text relevance and visual cosine similarity.
        Missing visual signals degrade to text-only ranking.
        """
        if not hits:
            return []

        # Normalize text scores to [0, 1] before fusion.
        scores = [h.score for h in hits]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0
        text_norm = {h.product_id: (h.score - lo) / span for h in hits}

        # Obtain visual similarity, degrading to text-only ranking on failure.
        visual: dict[str, float] = {}
        try:
            from llm.vision import embed_image_with_provider
            from search.visual_index import get_visual_index

            if image_embedding is not None and hasattr(image_embedding, "result"):
                provider, query_vec = image_embedding.result()
            elif image_embedding is not None:
                provider, query_vec = image_embedding
            else:
                provider, query_vec = embed_image_with_provider(image)
            visual = get_visual_index(provider).score_many(
                query_vec, [h.product_id for h in hits]
            )
        except Exception as exc:  # noqa: BLE001 - visual reranking is optional
                logger.warning("Visual reranking failed; falling back to text-only ordering: %r", exc)

        def _fused(h: ProductHit) -> float:
            t = text_norm.get(h.product_id, 0.0)
            v = visual.get(h.product_id)
            if v is None:
                return t  # No visual signal: use text relevance only.
            return _VISUAL_TEXT_WEIGHT * t + _VISUAL_IMAGE_WEIGHT * v

        ranked = sorted(hits, key=_fused, reverse=True)
        return ranked[:top_k]


    # ------------------------------------------------------------------
    # Single-demand path.
    # ------------------------------------------------------------------
    def _run_contextual(
        self,
        query: str,
        plan: ContextualSearchPlan,
        session: AgentSession,
        top_k: int,
    ) -> ToolResult:
        """Run complement or pivot retrieval without filtering by the old category."""
        pool_k = max(top_k * _DIVERSITY_POOL_MULTIPLIER, 20)
        result = self._search(
            self._get_service(),
            plan.target_query,
            session,
            top_k_products=pool_k,
            base=None,
        )

        hits: list[ProductHit] = []
        for h in self._apply_budget_fit_bonus(result.hits, result.parsed):
            if h.sub_category in plan.exclude_sub_categories:
                continue
            if plan.target_sub_categories and h.sub_category not in plan.target_sub_categories:
                continue
            hits.append(h)
            if len(hits) >= top_k:
                break

        session.remember_search(
            result.parsed.to_dict(),
            [{"product_id": h.product_id, "title": h.title} for h in hits],
        )

        products = [_card_with_evidence(h, query) for h in hits]
        self._attach_price_displays(products, result.parsed)

        summary = {
            "hit_count": len(hits),
            "needs_clarification": False,
            "category": result.parsed.category,
            "max_price": result.parsed.max_price,
            "mode": plan.mode,
        }
        payload = {
            "query": query,
            "products": products,
            "summary": summary,
            "contextual_search": plan.to_dict(),
            "debug": {
                "parsed": result.parsed.to_dict(),
                "raw_chunk_count": result.raw_chunk_count,
                "filtered_chunk_count": result.filtered_chunk_count,
                "target_query": plan.target_query,
                "excluded_sub_categories": plan.exclude_sub_categories,
                "target_sub_categories": plan.target_sub_categories,
                "hits_full": [h.to_dict() for h in hits],
            },
        }

        if not hits:
            if plan.mode == "complement":
                hint = (
                    f"用户想找可搭配「{plan.anchor_sub_category or plan.anchor_query or '上一轮商品'}」"
                    f"的「{plan.target_query}」，但目标品类未命中。请坦诚说明没有找到，"
                    "不要回退推荐上一轮品类，并建议换一个搭配方向。"
                )
            else:
                hint = (
                    f"用户想从上一轮切换到「{plan.target_query}」，但未命中。"
                    "请坦诚说明没有找到，不要回退推荐上一轮品类。"
                )
        elif plan.mode == "complement":
            hint = (
                f"这是搭配推荐：用户想找可搭配「{plan.anchor_sub_category or plan.anchor_query or '上一轮商品'}」"
                f"的「{plan.target_query}」。请只介绍命中的目标商品，不要把锚点当作推荐商品。"
            )
        else:
            hint = f"用户从上一轮切换到「{plan.target_query}」。请按新目标介绍命中商品，不要沿用旧品类。"

        return ToolResult(tool_name=self.name, payload=payload, composer_hint=hint)

    def _run_more(
        self,
        query: str,
        label: str,
        session: AgentSession,
        top_k: int,
        base: Any,
    ) -> ToolResult:
        """"看更多"：沿用上一轮的搜索条件，排除已经展示过的商品。

        分区名是模型写的小标题，再过一遍 query 解析可能被误判成新约束，所以直接并入
        上一轮的结构化意图，也省一次 LLM。多需求检索的基线只记了第一个需求，分区名
        可能属于别的需求，这时按分区名重新检索。"""
        if isinstance(base, ParsedQuery) and not session.task.multi_search:
            parsed = base
            if label:
                parsed = replace(
                    base,
                    soft_terms=list(dict.fromkeys([*base.soft_terms, label])),
                    retrieval_query=f"{label} {base.retrieval_query}".strip(),
                )
            return self._run_single(
                parsed.retrieval_query or parsed.original_query or query,
                session,
                top_k=top_k,
                base=None,
                parsed=parsed,
                exclude_seen=True,
            )
        return self._run_single(label or query, session, top_k=top_k, base=None, exclude_seen=True)

    def _run_single(
        self,
        query: str,
        session: AgentSession,
        top_k: int,
        base: Any,
        parsed: ParsedQuery | None = None,
        exclude_seen: bool = False,
    ) -> ToolResult:
        # 宽泛浏览（首轮、未承接 refine）时多召回一个候选池，便于做多样性筛选；
        # refine（base 非空）是聚焦的承接式追问，保持原样不打散。
        want_diversity = base is None and parsed is None
        pool_k = top_k * _DIVERSITY_POOL_MULTIPLIER if want_diversity else top_k
        search_kwargs: dict[str, Any] = {"top_k_products": pool_k, "base": base}
        if parsed is not None:
            search_kwargs["parsed"] = parsed
        seen = set(session.recall_seen()) if exclude_seen else set()
        if seen:
            # 已展示的商品会占掉召回名额，按数量放大，保证去重后还有新商品可选
            search_kwargs["top_k_products"] = pool_k + len(seen)
            search_kwargs["top_k_chunks"] = _MORE_BASE_CHUNKS + _MORE_CHUNKS_PER_SEEN * len(seen)
        result = self._search(
            self._get_service(),
            query,
            session,
            **search_kwargs,
        )
        if seen:
            result = replace(result, hits=[h for h in result.hits if h.product_id not in seen])

        # Apply diversity only when the user did not specify a brand.
        if want_diversity and not getattr(result.parsed, "brand_include", None):
            caps: list[tuple[Callable[[ProductHit], str], int]] = [
                (lambda h: _canonical_brand(h.brand), _MAX_PER_BRAND),
            ]
            # Add a subcategory cap only when browsing an entire category.
            if not getattr(result.parsed, "sub_category", None):
                caps.append((lambda h: h.sub_category or "", _MAX_PER_SUBCATEGORY))
            hits = _diversify(self._apply_budget_fit_bonus(result.hits, result.parsed), top_k, caps)
        else:
            hits = self._apply_budget_fit_bonus(result.hits, result.parsed)[:top_k]

        # 工作记忆（WorkingMemory 契约）：refine/compare/detail 后续会读这俩字段。
        # 存的是【结构化】ParsedQuery（dict），下一轮才能做无损约束叠加。
        # "看更多"没有新商品时不覆盖：用户说"第一个"指的还是屏幕上那批。
        if hits or not exclude_seen:
            session.remember_search(
                result.parsed.to_dict(),
                [{"product_id": h.product_id, "title": h.title} for h in hits],
                keep_seen=exclude_seen,
            )

        products = [_card_with_evidence(h, query) for h in hits]
        self._attach_price_displays(products, result.parsed)

        summary = {
            "hit_count": len(hits),
            "needs_clarification": result.parsed.needs_clarification,
            "category": result.parsed.category,
            "max_price": result.parsed.max_price,
            "unavailable_terms": list(getattr(result.parsed, "unavailable_terms", None) or []),
        }

        payload = {
            "query": query,
            "products": products,
            "summary": summary,
            "personalization": list(getattr(result, "personalization", None) or []),
            # debug 块给开发者排查用，前端可忽略
            "debug": {
                "parsed": result.parsed.to_dict(),
                "raw_chunk_count": result.raw_chunk_count,
                "filtered_chunk_count": result.filtered_chunk_count,
                "hits_full": [h.to_dict() for h in hits],
            },
        }

        # 给 composer 一点 hint，让它对零命中、需澄清做不同应答
        unavailable = "、".join(getattr(result.parsed, "unavailable_terms", None) or [])
        if exclude_seen and not hits:
            hint = "符合条件的商品都已经给用户看过了，没有新的可推荐。请坦诚说明，不要重复推荐，追问给放宽条件的方向。"
        elif exclude_seen:
            hint = (
                f"用户想看更多，这 {len(hits)} 款都是之前没展示过的。开场白要点明和刚才的不重复，"
                "每款写约 40 字的总结，说清核心卖点并补上适用人群或场景。"
            )
        elif result.parsed.needs_clarification:
            hint = "检索系统认为 query 过于模糊，请引导用户补充关键信息（品类、预算、用途）。"
        elif unavailable:
            hint = f"商品库里没有用户点名的「{unavailable}」。请坦诚说明没有，不要假装有，追问里给相近品类或品牌的方向。"
        elif not hits:
            hint = "本次未命中任何商品，请坦诚告知用户并给出可能放宽的方向建议。"
        else:
            hint = f"已为用户找到 {len(hits)} 款商品，请为每款写一句约 40 字的总结，说清核心卖点并补上适用人群或场景，不要只写一个短语。"
            memory_hint = _personalization_hint(payload["personalization"], session)
            if memory_hint:
                hint = f"{hint}{memory_hint}"
        if is_english():
            hint = english.search_hint(
                len(hits), showing_more=exclude_seen, vague=result.parsed.needs_clarification,
                unavailable=", ".join(getattr(result.parsed, "unavailable_terms", None) or []),
            )
            if hits:
                hint = f"{hint} {_personalization_hint(payload['personalization'], session)}".strip()

        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=hint,
        )

    # ------------------------------------------------------------------
    # Multi-demand fan-out and grouped merge.
    # ------------------------------------------------------------------
    def _run_multi(
        self,
        query: str,
        subs: list[SubRequest],
        session: AgentSession,
    ) -> ToolResult:
        service = self._get_service()

        # Run the full retrieval pipeline per subrequest. Assign duplicate products to the
        # first matching group only so the client does not render repeated cards.
        groups: list[dict[str, Any]] = []
        flat_products: list[dict[str, Any]] = []
        flat_hit_refs: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        first_parsed: dict[str, Any] | None = None

        for sub in subs:
            result: SearchResult = self._search(
                service,
                sub.query,
                session,
                top_k_products=PER_GROUP_TOP_K,
            )
            if first_parsed is None:
                first_parsed = result.parsed.to_dict()

            group_products: list[dict[str, Any]] = []
            for h in result.hits:
                if h.product_id in seen_ids:
                    continue
                seen_ids.add(h.product_id)
                card = _card_with_evidence(h, sub.query)
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

        # Store all merged hits and use the first parsed subrequest as a reasonable refine
        # baseline, since refinement over several groups is inherently ambiguous.
        session.remember_search(first_parsed or {}, flat_hit_refs)
        session.task.multi_search = True

        # Flat and grouped cards share objects, so one price-display pass updates both.
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
            "products": flat_products,   # Flat list retained for the existing contract.
            "groups": groups,            # Optional grouped rendering structure.
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
                "请【按需求分组】依次介绍，每组先点出需求名，"
                "然后极简地指出每款商品的核心卖点（15字以内，绝不重复商品名或价格）。"
            )

        if is_english():
            hint = english.MULTI_HINT if non_empty_groups else english.MULTI_EMPTY_HINT
        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=hint,
        )


# "看更多"的召回量：基础量 + 每个已展示商品再多召回这么多 chunk（一个商品有多个 chunk）。
_MORE_BASE_CHUNKS = 50
_MORE_CHUNKS_PER_SEEN = 10


def _personalization_hint(signals: list[dict[str, Any]], session: AgentSession) -> str:
    """告诉 composer 这次用到了哪些长期偏好，让回答能说清「为什么这样推荐」。"""
    labels = english.PERSONALIZATION_LABELS if is_english() else {
        "brand_avoid": "已避开用户不买的品牌",
        "avoid_term": "已避开用户忌讳的成分",
        "brand_like": "优先了用户喜欢的品牌",
        "brand_like_inferred": "参考了用户常看的品牌",
        "budget": "参考了用户的常用预算",
        "price_tier": "参考了用户的价格倾向",
    }
    parts = []
    for signal in signals:
        label = labels.get(signal.get("kind"))
        if not label:
            continue
        value = signal.get("value")
        named = signal["kind"] in {"brand_avoid", "avoid_term", "brand_like"}
        parts.append(f"{label} ({value})" if named and is_english() else f"{label}（{value}）" if named else label)
    profile = session.user_profile or {}
    context = []
    for scope, attrs in (profile.get("category_specific") or {}).items():
        if isinstance(attrs, dict):
            context.extend(str(v) for k, v in attrs.items() if k not in {"budget_min", "budget_max"} and v)
    note = profile.get("preference_note") or profile.get("notes")
    if note:
        context.append(str(note))
    if is_english():
        memory = f"You {', '.join(dict.fromkeys(parts))}; you may mention this briefly in the opening. " if parts else ""
        facts = f"Lasting facts about the shopper: {'; '.join(dict.fromkeys(context))}. Use them only where the product data supports it." if context else ""
        return (memory + facts).strip()
    text = ""
    if parts:
        text += "本次" + "，".join(dict.fromkeys(parts)) + "，可在开场白里自然带一句。"
    if context:
        text += "用户的长期情况：" + "；".join(dict.fromkeys(context)) + "，解说时可以结合，但不要编造商品没有的信息。"
    return text


def _card_with_evidence(h: ProductHit, query: str) -> dict[str, Any]:
    """商品卡片 + 给 composer 写解说用的证据（前端不用证据字段）。"""
    card = _to_product_card(h)
    checks = getattr(h, "attribute_checks", None)
    if checks:
        card["attribute_checks"] = dict(checks)
    claims = getattr(h, "graph_evidence", None)
    if claims:
        card["graph_evidence"] = list(claims)
    if getattr(h, "rating", None):
        card["rating"] = h.rating
    evidence = card_evidence(h, query)
    if evidence:
        card["evidence"] = evidence
    return card


def _to_product_card(h: ProductHit) -> dict[str, Any]:
    """Return the compact product-card fields required by the client."""
    return {
        "product_id": h.product_id,
        "title": h.title,
        "brand": h.brand,
        "category": h.category,
        "sub_category": h.sub_category,
        # price 保持为最低价（旧契约），区间在 price_range 里
        "price": _hit_price(h, "min_price"),
        "price_range": {
            "min": _hit_price(h, "min_price"),
            "max": _hit_price(h, "max_price"),
            "sku_count": int(getattr(h, "sku_count", 0) or 0),
        },
    }


def _hit_price(h: Any, field: str) -> float:
    value = getattr(h, field, None)
    if value is None:
        return float(getattr(h, "base_price", 0.0) or 0.0)
    return float(value)
