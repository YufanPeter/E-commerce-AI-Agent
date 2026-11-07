from __future__ import annotations

"""Conversational cart management and checkout.

This tool combines conversational CRUD, multi-turn state, and a complete purchase flow.

Architecture:
    The intent router sends cart/checkout requests here. ``dispatch_action`` selects
    add, remove, set_quantity, view, or checkout and extracts arguments. Deterministic
    code then applies the selected operation to the SQLite-backed ``CartStore``.

Reference resolution reuses the shared resolver. Demonstratives use the focused product
or recent hits, while cart ordinals refer to cart-line order, not recommendation order.

If LLM action dispatch fails, the safe fallback is to show the current cart.
"""

import dataclasses
import logging
from collections import OrderedDict
import re
from typing import Any

from agent.llm_actions import ActionSpec, dispatch_action
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.resolve import enrich_candidates, focus_index_in, resolve_one
from agent.tools.reference import attribute_matches, resolve_indices, resolve_by_name, extract_name_query
from store.cart_store import CartNotFoundError, CartStore, DEFAULT_ADDRESS, DEMO_USER_ID
from store.order_store import OrderStore
from store import sku_select
from store.product_store import ProductStore


logger = logging.getLogger(__name__)

# Cancellation terms that clear pending SKU selection without adding an item.
_CANCEL_WORDS = ("算了", "不加了", "不要了", "取消", "先不", "不用了", "不买了")


# 确定性识别「买过的东西」相关意图，不依赖模型，路由层也用它兜底。
_ORDER_HISTORY_RE = re.compile(r"买过(什么|哪些|啥|的东西)|买了(什么|哪些|啥)|买的(是)?(什么|啥|哪)|历史订单|我的订单|订单记录|下过(什么|哪些)")
_REORDER_WORDS = (
    "再买一次", "再买一单", "再来一单", "再下一单", "再下一遍", "再买一遍", "复购", "回购", "补货",
    "上次买的", "上回买的", "之前买的", "以前买的", "上一单", "买过的",
)
# 在问上次买的东西本身，不是要再买
_NOT_REORDER_WORDS = ("怎么样", "好不好", "好用吗", "评价", "差评", "对比", "比较", "多少钱", "退货", "退款", "类似", "替代", "同款以外")
_WHOLE_ORDER_WORDS = ("上一单", "整单", "全部", "都再", "那些", "一单", "一遍")
_REORDER_FILLER_RE = re.compile(
    r"再买一次|再买一单|再买一遍|再来一单|再下一单|再下一遍|复购|回购|补货|上一单|上次|上回|之前|以前|买的|买过的?|"
    r"那个|那款|那件|那瓶|那些|这些|这个|整单|全部|都|一次|一遍|一单|再来|再买|帮我|给我|麻烦|一下|"
    r"把|将|请|吧|了|的|我|想|要|"
    r"[0-9一两二三四五六七八九十]+\s*(瓶|件|个|盒|箱|袋|桶|双|条|支|份|罐|包|台|部)"
)
# 「第二个」是序号不是数量；「第」后面的数字不算。
_QUANTITY_RE = re.compile(r"(?<![第0-9])([0-9]+|[一两二三四五六七八九十])\s*(瓶|件|个|盒|箱|袋|桶|双|条|支|份|罐|包|台|部)")
_CN_DIGITS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def detect_order_intent(text: str) -> str | None:
    """返回 "history"、"reorder" 或 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if _ORDER_HISTORY_RE.search(text):
        return "history"
    if any(word in text for word in _REORDER_WORDS) and not any(word in text for word in _NOT_REORDER_WORDS):
        return "reorder"
    return None


def _quantity_in(text: str) -> int | None:
    match = _QUANTITY_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1)
    return int(raw) if raw.isdigit() else _CN_DIGITS.get(raw)


_ACTIONS = [
    ActionSpec(
        name="add",
        description="把某款商品加入购物车（'加入购物车'/'把这个/刚才那款加进来'/'来一件'）",
        parameters={
            "index": {
                "type": "integer",
                "description": "加入上一轮推荐里的第几个，从 1 开始；说'这款/刚才那个'时省略。",
            },
            "quantity": {"type": "integer", "description": "数量，默认 1。"},
        },
    ),
    ActionSpec(
        name="set_quantity",
        description="修改购物车里某件商品的数量（'第一件改成两个'/'多加一件'）",
        parameters={
            "cart_index": {"type": "integer", "description": "购物车里第几件，从 1 开始。"},
            "quantity": {"type": "integer", "description": "改成的目标数量。"},
        },
    ),
    ActionSpec(
        name="remove",
        description="从购物车删除某件商品（'删掉第二个'/'把 XX 去掉'/'不要第一件了'）",
        parameters={
            "cart_index": {"type": "integer", "description": "购物车里第几件，从 1 开始。"},
        },
    ),
    ActionSpec(
        name="view",
        description="查看当前购物车（'看看购物车'/'车里有啥'/'一共多少钱'）",
        parameters={},
    ),
    ActionSpec(
        name="checkout",
        description="提交订单/下单（'下单吧'/'结算'/'就买这些'）",
        parameters={
            "address": {"type": "string", "description": "收货地址；说'用默认的'时省略。"},
        },
    ),
    ActionSpec(
        name="reorder",
        description="把以前买过的东西再买一次（'再来一份之前那瓶'/'上回那个再拿两盒'）",
        parameters={},
    ),
    ActionSpec(
        name="order_history",
        description="查看以前的订单（'我之前都买了啥'/'看看历史订单'）",
        parameters={},
    ),
]

_PURPOSE = "管理用户的购物车（加购、改数量、删除、查看）并完成下单结算"


class CartTool:
    name: str = "cart"

    def __init__(
        self,
        cart_store: CartStore | None = None,
        product_store: ProductStore | None = None,
        order_store: OrderStore | None = None,
    ) -> None:
        self._store = cart_store or CartStore()
        self._orders = order_store or OrderStore(self._store.db_path)
        # 全库商品检索：用户"点名"加购（把小米/oppo 加进来）但当前推荐列表里
        # 没有该商品时，直接去整库按关键词定位，而不是回退到上一轮聚焦的商品。
        self._products = product_store or ProductStore()

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        # Use the raw utterance for product/SKU resolution because a router rewrite may
        # inject contextual product names. The rewritten query is used only for action type.
        raw_query = self._user_text(session, query)

        # Treat a turn as an SKU answer while a multi-SKU selection is pending.
        pending = session.get("pending_cart")
        if pending:
            return self._resolve_pending_sku(raw_query, session, pending)

        # Treat a turn as a product selection while an ambiguous add is pending.
        pending_add = session.get("pending_add")
        if pending_add:
            return self._resolve_pending_add(raw_query, session, pending_add)

        order_intent = detect_order_intent(raw_query)
        if order_intent:
            action, args = {"history": "order_history", "reorder": "reorder"}[order_intent], {}
        else:
            action, args = self._decide(query, session)
        logger.info("cart: action=%s args=%s", action, args)
        try:
            if action == "add":
                return self._add(raw_query, session, args)
            if action == "set_quantity":
                return self._set_quantity(args)
            if action == "remove":
                return self._remove(args)
            if action == "checkout":
                return self._checkout(args, session)
            if action == "reorder":
                return self._reorder(raw_query, session)
            if action == "order_history":
                return self._order_history(session)
            return self._view()  # view 及未知动作的安全兜底
        except CartNotFoundError as exc:
            return self._plain(str(exc) + "。要不先看看购物车或换一件？")

    # ------------------------------ Action dispatch ------------------------------

    @staticmethod
    def _user_text(session: AgentSession, fallback: str) -> str:
        """Return the raw utterance from the latest user history entry.

        The orchestrator stores it before routing, allowing name and SKU resolution to
        avoid unrelated product names introduced by rewriting.
        """
        for msg in reversed(session.history):
            if msg.role == "user":
                return msg.content
        return fallback

    def _decide(self, query: str, session: AgentSession) -> tuple[str, dict[str, Any]]:
        """Select an action, falling back to cart view when LLM dispatch fails."""
        try:
            decision = dispatch_action(query, session, _ACTIONS, purpose=_PURPOSE)
            return decision.action, decision.args
        except Exception as exc:  # noqa: BLE001
            logger.warning("cart action dispatch failed, fallback to view: %r", exc)
            return "view", {}

    # ------------------------------ Actions ------------------------------

    def _add(self, query: str, session: AgentSession, args: dict[str, Any]) -> ToolResult:
        """Resolve the product and begin the add-to-cart flow.

        Priority is explicit ordinal, current-list name, catalog lookup, focused product,
        then a sole candidate. Ambiguous candidates trigger clarification; an unrelated
        product is never selected silently.
        """
        last_hits = session.recall_hits()
        qty = _as_int(args.get("quantity"), default=1)

        # An explicitly named brand in the raw utterance is more reliable than an index
        # inferred from rewritten context, so calculate brand matches first.
        brand_hits = self._products.match_brands_in_text(query)
        brand_ids = {c.product_id for c in brand_hits}

        # 1) 显式序号：只采信用户原句里真的出现的"第N个/最后一个"。
        # dispatch_action 的 args.index 可能来自 router/LLM 对上下文的猜测；
        # 用户只说"帮我加入购物车"时若上一轮刚聚焦了小米，不能让这个猜测
        # 覆盖 last_focus_product_id，错加成列表第一个 iPad。
        parsed = resolve_indices(query, len(last_hits), focus_index=focus_index_in(last_hits, session))
        idx = (parsed[0] + 1) if parsed else None
        if idx is not None:
            i = int(idx) - 1
            if 0 <= i < len(last_hits):
                target_id = last_hits[i]["product_id"]
                # Accept the index only when it does not conflict with an explicit brand.
                if not brand_ids or target_id in brand_ids:
                    return self._begin_add(target_id, qty, session, query, source="explicit_index")

        # 2) 当前推荐列表里名称/品牌/规格值命中（快路径：屏幕上就有，直接定位）。
        #    候选先补上 SKU 规格值，「黑色那个加购物车」才能靠「黑色」定位到商品，
        #    之后 _begin_add 会再用同一句话把黑色这个规格也选上。
        enriched_hits = self._enrich_candidates(last_hits)
        named = resolve_by_name(query, enriched_hits)
        if len(named) == 1:
            return self._begin_add(last_hits[named[0]]["product_id"], qty, session, query, source="context_name")
        if len(named) > 1:
            # 同品牌多品类（「华为耳机」命中华为耳机+华为手机）→ 先让 LLM 按子品类
            # 语义挑唯一一款，挑不出再列候选反问，避免反复追问。
            subset = [enriched_hits[i] for i in named]
            picked = resolve_one(query, subset)
            if picked is not None:
                return self._begin_add(subset[picked]["product_id"], qty, session, query, source="semantic_context_match")
            return self._ask_which_product(subset, session)

        # 2.5) 没点名、只说了个规格值（「42 码的加进来」「黑色的来一件」）：
        #      指向刚聚焦（详情介绍过 / 刚加购过）的那一款，规格交给 _begin_add 用同一句话选。
        focus = session.get("last_focus_product_id")
        if focus and not brand_ids:
            focus_hit = next((h for h in enriched_hits if str(h.get("product_id")) == str(focus)), None)
            if focus_hit is not None and attribute_matches(query, focus_hit):
                return self._begin_add(str(focus), qty, session, query, source="focus_with_spec")

        # 3) 全库点名检索（text2sql 思路）：先拿原句直接和库里品牌名做子串匹配，
        #    "请你把小米加入购物车"这种带语气前缀也能稳稳命中"小米"；品牌没命中
        #    再退回抽词按 title 搜型号/品类（如"北面冲锋衣"）。覆盖所有品类。
        keyword = extract_name_query(query)
        found = brand_hits
        if not found and keyword:
            found = self._products.search_by_keyword(keyword)
        if len(found) == 1:
            source = "brand_match" if brand_hits else "keyword_match"
            return self._begin_add(found[0].product_id, qty, session, query, source=source)
        if len(found) > 1:
            hits = [{"product_id": c.product_id, "title": c.title} for c in found]
            # Store catalog matches so subsequent turns can refer to them.
            session.set("last_hits", hits)
            return self._ask_which_product(hits, session)

        # 4. Use the focused product for an unnamed demonstrative request.
        focus = session.get("last_focus_product_id")
        if focus and not keyword:
            return self._begin_add(focus, qty, session, query, source="focus")

        # 5. A named but missing product must not fall back to an unrelated visible item.
        if keyword or brand_ids:
            return self._plain(
                f"没有找到和「{keyword or query}」匹配的商品哦，"
                "换个说法或者先让我帮你推荐几款？"
            )

        # 6. Default only when a single candidate exists; otherwise clarify.
        if len(last_hits) == 1:
            return self._begin_add(last_hits[0]["product_id"], qty, session, query, source="only_candidate")
        if last_hits:
            return self._ask_which_product(last_hits, session)

        return self._plain(
            "想把哪款加进来呀？可以说「把第一个加入购物车」，"
            "或者先让我推荐几款。"
        )

    def _ask_which_product(
        self, hits: list[dict[str, Any]], session: AgentSession
    ) -> ToolResult:
        """List candidates when reference resolution cannot identify one product."""
        candidates = [
            {"product_id": h["product_id"], "title": h.get("title", "")} for h in hits
        ]
        session.set("pending_add", {"candidates": candidates})
        lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(candidates)]
        text = (
            "你想加入哪一款呢？\n"
            + "\n".join(lines)
            + "\n告诉我序号或品牌名就行，例如「第一个」或「华为那款」。"
        )
        return ToolResult(
            tool_name=self.name,
            payload={"action": "ask_which_product", "candidates": candidates},
            narrative_override=text,
            needs_composer=False,
        )

    def _enrich_candidates(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Enrich candidates with catalog fields for semantic disambiguation.

        富化失败（取详情异常）不影响主流程——LLM 仍可只凭 title 消歧。"""
        return enrich_candidates(candidates, self._products)

    def _resolve_pending_add(
        self, query: str, session: AgentSession, pending_add: dict[str, Any]
    ) -> ToolResult:
        """Resolve an answer to a pending product choice, or ask again."""
        if self._is_cancel(query):
            session.set("pending_add", None)
            return self._plain("好的，先不加了。还有什么我可以帮你的？")

        candidates: list[dict[str, Any]] = pending_add.get("candidates", [])
        if not candidates:
            session.set("pending_add", None)
            return self._plain("想加哪款呢？先让我帮你推荐几款吧～")

        if pending_add.get("reorder"):
            picked = resolve_one(query, _reorder_candidates(candidates))
            if picked is None:
                return self._ask_which_reorder(candidates, session, pending_add.get("quantity"))
            session.set("pending_add", None)
            quantity = _quantity_in(query) or pending_add.get("quantity")
            return self._commit_reorder(candidates[picked], quantity, session)

        # 用户改点了候选之外的别的品牌商品 → 放弃当前候选，重新定位。
        switched = self._switch_intent(
            query, session, [c["product_id"] for c in candidates]
        )
        if switched is not None:
            return switched

        # Shared layered resolution: ordinal, unique name, then semantic disambiguation.
        picked = resolve_one(query, self._enrich_candidates(candidates))
        if picked is None:
            # Preserve candidates and ask again when unresolved.
            return self._ask_which_product(candidates, session)

        session.set("pending_add", None)
        return self._begin_add(candidates[picked]["product_id"], 1, session, query, source="pending_add")

    def _switch_intent(
        self, query: str, session: AgentSession, current_ids: list[str]
    ) -> ToolResult | None:
        """Detect a switch to another brand during pending product/SKU selection.

        A direct brand substring whose product is outside the pending set triggers a
        restart. SKU values cannot trigger this brand-based test.
        """
        # Match raw text directly against catalog brands, including polite prefixes.
        found = self._products.match_brands_in_text(query)
        if not found:
            return None
        found_ids = {c.product_id for c in found}
        if found_ids & set(current_ids):
            return None  # The same brand remains within the current selection.
        session.set("pending_cart", None)
        session.set("pending_add", None)
        return self._add(query, session, {})

    def _begin_add(
        self,
        product_id: str,
        qty: int,
        session: AgentSession,
        query: str,
        source: str = "unknown",
    ) -> ToolResult:
        """Add a single SKU directly or begin multi-SKU clarification."""
        resolution = self._build_resolution(product_id, query, source)
        skus = self._store.list_skus(product_id)
        if not skus:
            return self._plain("这款暂时没有可购买的规格了，换一款看看？")

        if len(skus) == 1:
            return self._commit_add(product_id, skus[0]["sku_id"], qty, resolution=resolution)

        # Check whether the current utterance already identifies one SKU.
        matched = self._filter_skus(query, skus)
        if len(matched) == 1:
            return self._commit_add(product_id, matched[0]["sku_id"], qty, resolution=resolution)

        # Store pending state and accumulated specification text for later filtering.
        title = self._store.product_title(product_id)
        session.set("pending_cart", {
            "product_id": product_id,
            "title": title,
            "quantity": qty,
            "spec_text": query,
            "resolution": resolution,
        })
        return self._ask_spec(product_id, title, matched if matched else skus, resolution=resolution)

    def _build_resolution(self, product_id: str, query: str, source: str) -> dict[str, Any]:
        """Record the resolved product for diagnostics and confirmation copy."""
        title = self._store.product_title(product_id)
        return {
            "original_query": query,
            "resolved_query": f"把「{title}」加入购物车",
            "product_id": product_id,
            "title": title,
            "source": source,
        }

    def _resolve_pending_sku(
        self, query: str, session: AgentSession, pending: dict[str, Any]
    ) -> ToolResult:
        """Resolve a pending SKU answer and add it, or continue clarification."""
        if self._is_cancel(query):
            session.set("pending_cart", None)
            return self._plain("好的，先不加了。还有什么我可以帮你的？")

        # Restart product resolution when another brand is named during SKU selection.
        switched = self._switch_intent(query, session, [pending["product_id"]])
        if switched is not None:
            return switched

        product_id = pending["product_id"]
        qty = _as_int(pending.get("quantity"), default=1)
        skus = self._store.list_skus(product_id)
        if not skus:
            session.set("pending_cart", None)
            return self._plain("这款规格信息好像丢失了，麻烦重新说一次要加哪款～")

        # Combine previous and current specification terms to narrow dimensions gradually.
        combined = f"{pending.get('spec_text', '')} {query}".strip()
        matched = self._filter_skus(combined, skus)
        if len(matched) != 1:
            picked = self._match_ordinal(query, matched if matched else skus)
            if picked is not None:
                matched = [picked]

        if len(matched) == 1:
            session.set("pending_cart", None)
            return self._commit_add(
                product_id,
                matched[0]["sku_id"],
                qty,
                resolution=pending.get("resolution"),
            )

        # Preserve accumulated terms and ask about remaining dimensions.
        pending = {**pending, "spec_text": combined}
        session.set("pending_cart", pending)
        return self._ask_spec(
            product_id,
            pending["title"],
            matched if matched else skus,
            resolution=pending.get("resolution"),
        )

    def _commit_add(
        self,
        product_id: str,
        sku_id: str,
        qty: int,
        resolution: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Persist a resolved SKU and return the confirmation result.

        This deterministic action uses concise local copy, avoiding another LLM call.
        """
        line = self._store.add_product(product_id, sku_id=sku_id, quantity=qty)
        snapshot = self._snapshot()
        spec = "、".join(f"{k}{v}" for k, v in line.options.items()) if line.options else ""
        spec_text = f"（{spec}）" if spec else ""
        text = (
            f"已把「{line.title}」{spec_text}加入购物车，数量 {line.quantity}。"
            f"购物车现共 {snapshot['cart']['item_count']} 件，"
            f"合计 {snapshot['cart']['total']} 元。"
        )
        return ToolResult(
            tool_name=self.name,
            payload={
                "action": "add",
                "added": line.to_dict(),
                **({"resolution": resolution} if resolution else {}),
                **snapshot,
            },
            narrative_override=text,
            needs_composer=False,
        )

    def _ask_spec(
        self,
        product_id: str,
        title: str,
        skus: list[dict[str, Any]],
        resolution: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Ask for varying SKU dimensions instead of listing every combination."""
        dims = self._varying_dims(skus)
        if not dims:
            # Defensive fallback for multiple SKUs with no varying dimension.
            return self._commit_add(product_id, skus[0]["sku_id"], 1)

        # Let the client render options; keep narrative copy concise.
        text = f"已理解为把「{title}」加入购物车。这款有多个规格可选，下面选一下你想要的款式吧～"
        # Ordered dimensions produce stable chips; retain ``options`` for compatibility.
        dimensions = [{"name": dim, "values": values} for dim, values in dims.items()]
        return ToolResult(
            tool_name=self.name,
            payload={
                "action": "ask_spec",
                "product_id": product_id,
                "title": title,
                "dimensions": dimensions,
                "options": dims,
                **({"resolution": resolution} if resolution else {}),
            },
            narrative_override=text,
            needs_composer=False,
        )

    # ------------------------------ SKU parsing ------------------------------

    @staticmethod
    def _varying_dims(skus: list[dict[str, Any]]) -> "OrderedDict[str, list[str]]":
        return sku_select.varying_dims(skus)

    @staticmethod
    def _sort_values(values: list[str]) -> list[str]:
        return sku_select.sort_values(values)

    def _filter_skus(
        self, query: str, skus: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """按用户这句话里能识别到的规格值过滤 SKU（实现见 store.sku_select）。"""
        return sku_select.filter_skus(query, skus)

    @staticmethod
    def _pick_dim_value(values: list[str], query: str) -> str | None:
        return sku_select.pick_dim_value(values, query)

    @staticmethod
    def _value_match_score(value: str, query: str) -> int:
        return sku_select.value_match_score(value, query)

    @staticmethod
    def _match_ordinal(
        query: str, skus: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Support ordinal selection when the SKU set is small."""
        if len(skus) > 9:
            return None
        idxs = resolve_indices(query, len(skus))
        if idxs and 0 <= idxs[0] < len(skus):
            return skus[idxs[0]]
        return None

    @staticmethod
    def _is_cancel(query: str) -> bool:
        return any(word in query for word in _CANCEL_WORDS)

    def _set_quantity(self, args: dict[str, Any]) -> ToolResult:
        line = self._target_line(args.get("cart_index"))
        qty = _as_int(args.get("quantity"), default=None)
        if qty is None:
            return self._plain("要改成几件呢？比如「第一件改成 2 个」。")
        updated = self._store.set_quantity(line.cart_item_id, qty)
        snapshot = self._snapshot()
        if updated is None:
            hint = f"已把「{line.title}」从购物车移除。"
        else:
            hint = f"已把「{updated.title}」数量改为 {updated.quantity}。"
        return ToolResult(
            tool_name=self.name,
            payload={"action": "set_quantity", **snapshot},
            composer_hint=hint + f"请确认并带出当前合计 {snapshot['cart']['total']} 元。",
        )

    def _remove(self, args: dict[str, Any]) -> ToolResult:
        line = self._target_line(args.get("cart_index"))
        self._store.remove_item(line.cart_item_id)
        snapshot = self._snapshot()
        return ToolResult(
            tool_name=self.name,
            payload={"action": "remove", "removed": line.to_dict(), **snapshot},
            composer_hint=(
                f"已把「{line.title}」从购物车删除。"
                f"请确认并带出当前还剩 {snapshot['cart']['item_count']} 件、"
                f"合计 {snapshot['cart']['total']} 元。"
            ),
        )

    def _view(self) -> ToolResult:
        snapshot = self._snapshot()
        if not snapshot["cart"]["lines"]:
            return self._plain("你的购物车还是空的，先让我帮你推荐几款吧～")
        return ToolResult(
            tool_name=self.name,
            payload={"action": "view", **snapshot},
            composer_hint=(
                "请用自然口吻汇报购物车：逐件说商品名、数量、小计，"
                f"最后给出合计 {snapshot['cart']['total']} 元，并询问是否下单。"
            ),
        )

    def _checkout(self, args: dict[str, Any], session: AgentSession) -> ToolResult:
        address = args.get("address") or DEFAULT_ADDRESS
        order = self._store.build_order(address=address, order_user_id=self._order_user(session))
        return ToolResult(
            tool_name=self.name,
            payload={"action": "checkout", "order": order.to_dict()},
            composer_hint=(
                f"下单成功！订单号 {order.order_id}，共 {order.to_dict()['item_count']} 件、"
                f"合计 {order.total} 元，寄往：{order.address}。"
                "请用愉快的口吻确认下单完成。"
            ),
        )

    # ------------------------------ 买过的东西 ------------------------------

    @staticmethod
    def _order_user(session: AgentSession) -> str:
        return session.user_id or DEMO_USER_ID

    def _reorder(self, query: str, session: AgentSession) -> ToolResult:
        """把买过的东西加回购物车。不直接下单：现价可能变了，要用户再确认一次。"""
        user_id = self._order_user(session)
        orders = self._orders.list_orders(user_id)
        if not orders:
            return self._plain("还没有找到你的下单记录，告诉我想买什么，我帮你找。")

        quantity = _quantity_in(query)
        wants_whole_order = any(word in query for word in _WHOLE_ORDER_WORDS)
        named = _REORDER_FILLER_RE.sub("", query).strip(" ，,。.！!？?")
        candidates = self._orders.recent_products(user_id)

        if not named:
            last_lines = orders[0]["lines"]
            if wants_whole_order and len(last_lines) > 1:
                return self._commit_whole_order(orders[0], session)
            if len(last_lines) == 1:
                return self._commit_reorder(last_lines[0], quantity, session)
            return self._ask_which_reorder(last_lines, session, quantity)

        if len(candidates) == 1:
            matched = candidates
        else:
            picked = resolve_one(query, _reorder_candidates(candidates))
            matched = [candidates[picked]] if picked is not None else []
        if matched:
            return self._commit_reorder(matched[0], quantity, session)
        titles = "、".join(line["title"][:14] for line in candidates[:3])
        return self._plain(f"你的订单里没找到「{named}」，最近买过的有：{titles}。想再买哪个？")

    def _ask_which_reorder(
        self,
        lines: list[dict[str, Any]],
        session: AgentSession,
        quantity: int | None = None,
    ) -> ToolResult:
        candidates = lines[:5]
        session.set("pending_add", {"candidates": candidates, "reorder": True, "quantity": quantity})
        rows = [
            f"{i + 1}. {line['title'][:24]}{_spec_suffix(line)} · 上次 ¥{line['unit_price']:g}"
            for i, line in enumerate(candidates)
        ]
        return ToolResult(
            tool_name=self.name,
            payload={"action": "ask_which_product", "candidates": candidates},
            narrative_override="你想再买哪一个？\n" + "\n".join(rows) + "\n说序号或商品名就行。",
            needs_composer=False,
        )

    def _price_note(self, previous: float, current: float) -> str:
        if abs(previous - current) < 0.01:
            return f"现价 ¥{current:g}，和上次一样"
        direction = "便宜" if current < previous else "贵"
        return f"现价 ¥{current:g}，比上次{direction} ¥{abs(current - previous):g}"

    def _commit_reorder(
        self,
        line: dict[str, Any],
        quantity: int | None,
        session: AgentSession,
    ) -> ToolResult:
        qty = quantity or int(line.get("quantity") or 1)
        product_id = line["product_id"]
        try:
            added = self._store.add_product(product_id, sku_id=line["sku_id"], quantity=qty)
        except CartNotFoundError:
            # 上次的规格下架了：商品还在就让用户重新选规格，不擅自换成别的规格
            if self._store.list_skus(product_id):
                result = self._begin_add(product_id, qty, session, "", source="reorder")
                prefix = f"上次买的{_spec_suffix(line) or '规格'}已经下架了。"
                return dataclasses.replace(result, narrative_override=prefix + (result.narrative_override or ""))
            return self._plain(f"「{line['title'][:20]}」已经下架了，要帮你找类似的吗？")
        session.set("last_focus_product_id", product_id)
        snapshot = self._snapshot()
        current = next(
            (s["price"] for s in self._store.list_skus(product_id) if s["sku_id"] == line["sku_id"]),
            added.unit_price,
        )
        text = (
            f"已把上次买的「{line['title'][:24]}」{_spec_suffix(line)}×{qty} 加入购物车，"
            f"{self._price_note(float(line['unit_price']), float(current))}。"
            f"购物车合计 {snapshot['cart']['total']:g} 元，要直接下单吗？"
        )
        return ToolResult(
            tool_name=self.name,
            payload={
                "action": "add",
                "added": added.to_dict(),
                "reorder": {"previous_price": line["unit_price"], "current_price": current},
                **snapshot,
            },
            narrative_override=text,
            needs_composer=False,
        )

    def _commit_whole_order(self, order: dict[str, Any], session: AgentSession) -> ToolResult:
        added: list[dict[str, Any]] = []
        skipped: list[str] = []
        for line in order["lines"]:
            try:
                added.append(
                    self._store.add_product(line["product_id"], sku_id=line["sku_id"], quantity=int(line["quantity"])).to_dict()
                )
            except CartNotFoundError:
                skipped.append(line["title"][:16])
        if not added:
            return self._plain("上一单的商品都已经下架了，要帮你找类似的吗？")
        snapshot = self._snapshot()
        previous = round(sum(float(l["unit_price"]) * int(l["quantity"]) for l in order["lines"]), 2)
        text = f"已把上一单的 {len(added)} 件商品加入购物车，购物车合计 {snapshot['cart']['total']:g} 元（上一单 {previous:g} 元）。"
        if skipped:
            text += f"其中「{'、'.join(skipped)}」已下架，没有加入。"
        return ToolResult(
            tool_name=self.name,
            payload={"action": "reorder", "added_lines": added, **snapshot},
            narrative_override=text + "要直接下单吗？",
            needs_composer=False,
        )

    def _order_history(self, session: AgentSession) -> ToolResult:
        orders = self._orders.list_orders(self._order_user(session), limit=5)
        if not orders:
            return self._plain("你还没有下过单，先看看想买什么吧～")
        rows: list[str] = []
        hits: list[dict[str, str]] = []
        for order in orders:
            names = "、".join(f"{line['title'][:14]}×{line['quantity']}" for line in order["lines"][:3])
            more = f" 等 {len(order['lines'])} 件" if len(order["lines"]) > 3 else ""
            rows.append(f"{order['created_at'][:10]}：{names}{more}，共 ¥{order['total']:g}")
            for line in order["lines"]:
                if all(hit["product_id"] != line["product_id"] for hit in hits):
                    hits.append({"product_id": line["product_id"], "title": line["title"]})
        # 接下来说「第一个详细说说」「再买第二个」能指到这些商品
        session.set("last_hits", hits)
        return ToolResult(
            tool_name=self.name,
            payload={"action": "order_history", "orders": orders},
            narrative_override="你最近的订单：\n" + "\n".join(rows) + "\n想再买哪个，直接告诉我。",
            needs_composer=False,
        )

    # ------------------------------ 辅助 ------------------------------

    def _target_line(self, cart_index: Any):
        """Resolve a cart ordinal to a line, defaulting to the first line."""
        lines = self._store.list_items()
        if not lines:
            raise CartNotFoundError("购物车是空的")
        i = (int(cart_index) - 1) if cart_index is not None else 0
        if not (0 <= i < len(lines)):
            raise CartNotFoundError(f"购物车里没有第 {cart_index} 件")
        return lines[i]

    def _snapshot(self) -> dict[str, Any]:
        lines = self._store.list_items()
        return {
            "cart": {
                "lines": [line.to_dict() for line in lines],
                "item_count": sum(line.quantity for line in lines),
                "total": round(sum(line.subtotal for line in lines), 2),
            }
        }

    def _plain(self, text: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"action": "noop"},
            narrative_override=text,
            needs_composer=False,
        )


def _reorder_candidates(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """订单行转成消歧用的候选：规格值要是列表，和 enrich_candidates 的结构一致。"""
    return [
        {
            "product_id": line["product_id"],
            "title": line.get("title", ""),
            "brand": line.get("brand", ""),
            "category": line.get("category", ""),
            "sub_category": line.get("sub_category", ""),
            "options": {str(k): [str(v)] for k, v in (line.get("options") or {}).items()},
        }
        for line in lines
    ]


def _spec_suffix(line: dict[str, Any]) -> str:
    options = line.get("options") or {}
    return f"（{'、'.join(str(v) for v in options.values())}）" if options else ""


def _as_int(value: Any, default: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
