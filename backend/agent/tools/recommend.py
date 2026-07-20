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
from typing import Any, Callable

from agent.contextual_search import ContextualSearchPlan, detect_contextual_plan
from agent.session import AgentSession
from agent.tools.base import ToolResult
from search.query_decomposer import SubRequest, decompose_query
from search.query_understanding import ParsedQuery
from search.search_service import ProductHit, SearchResult, SearchService


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

# Normalize multilingual brand aliases so they share the same diversity quota. Keys are
# lowercase and whitespace-free; values are canonical brand identifiers.
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

    def _attach_price_displays(self, cards: list[dict[str, Any]]) -> None:
        """Attach deterministic, preformatted price displays to product cards.

        Real ranges prevent the composer from presenting a multi-SKU product as one exact
        price. Fact lookup failures fall back to a single displayed price.
        """
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
        # RefineTool injects ``base_parsed`` so current constraints merge with prior intent.
        base = slots.get("base_parsed")

        # For complements and pivots, the previous category is context, not a hard filter.
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
    ) -> ToolResult:
        """Search by image using VLM terms and the source image URL or base64 data.

        Retrieve a text candidate pool, calculate visual similarity, fuse the scores, and
        return the top product cards. Missing visual data degrades to text order.
        """
        pool = max(top_k, _VISUAL_POOL_SIZE)
        result = self._search(
            self._get_service(),
            query,
            session,
            top_k_products=pool,
        )

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
            from llm.vision import embed_image
            from search.visual_index import get_visual_index

            query_vec = embed_image(image)
            visual = get_visual_index().score_many(
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
        for h in result.hits:
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

        products = [_to_product_card(h) for h in hits]
        self._attach_price_displays(products)

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

    def _run_single(
        self,
        query: str,
        session: AgentSession,
        top_k: int,
        base: Any,
    ) -> ToolResult:
        # Retrieve a wider pool for broad browsing; keep focused refinements unchanged.
        want_diversity = base is None
        pool_k = top_k * _DIVERSITY_POOL_MULTIPLIER if want_diversity else top_k
        result = self._search(
            self._get_service(),
            query,
            session,
            top_k_products=pool_k,
            base=base,
        )

        # Apply diversity only when the user did not specify a brand.
        if want_diversity and not getattr(result.parsed, "brand_include", None):
            caps: list[tuple[Callable[[ProductHit], str], int]] = [
                (lambda h: _canonical_brand(h.brand), _MAX_PER_BRAND),
            ]
            # Add a subcategory cap only when browsing an entire category.
            if not getattr(result.parsed, "sub_category", None):
                caps.append((lambda h: h.sub_category or "", _MAX_PER_SUBCATEGORY))
            hits = _diversify(result.hits, top_k, caps)
        else:
            hits = result.hits[:top_k]

        # Store structured intent and hit references for later refinement and resolution.
        session.remember_search(
            result.parsed.to_dict(),
            [{"product_id": h.product_id, "title": h.title} for h in hits],
        )

        products = [_to_product_card(h) for h in hits]
        self._attach_price_displays(products)

        summary = {
            "hit_count": len(hits),
            "needs_clarification": result.parsed.needs_clarification,
            "category": result.parsed.category,
            "max_price": result.parsed.max_price,
        }

        payload = {
            "query": query,
            "products": products,
            "summary": summary,
            # Developer diagnostics; clients may ignore this block.
            "debug": {
                "parsed": result.parsed.to_dict(),
                "raw_chunk_count": result.raw_chunk_count,
                "filtered_chunk_count": result.filtered_chunk_count,
                "hits_full": [h.to_dict() for h in hits],
            },
        }

        # Guide composition for clarification, empty results, and successful retrieval.
        if result.parsed.needs_clarification:
            hint = "检索系统认为 query 过于模糊，请引导用户补充关键信息（品类、预算、用途）。"
        elif not hits:
            hint = "本次未命中任何商品，请坦诚告知用户并给出可能放宽的方向建议。"
        else:
            hint = f"已为用户找到 {len(hits)} 款商品，请为每款写一句约 30 字的总结，说清核心卖点并补上适用人群或场景，不要只写一个短语。"

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

        # Store all merged hits and use the first parsed subrequest as a reasonable refine
        # baseline, since refinement over several groups is inherently ambiguous.
        session.remember_search(first_parsed or {}, flat_hit_refs)

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

        return ToolResult(
            tool_name=self.name,
            payload=payload,
            composer_hint=hint,
        )


def _to_product_card(h: ProductHit) -> dict[str, Any]:
    """Return the compact product-card fields required by the client."""
    return {
        "product_id": h.product_id,
        "title": h.title,
        "brand": h.brand,
        "category": h.category,
        "sub_category": h.sub_category,
        "price": h.base_price,
    }
