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
from agent.tools.resolve import (
    attribute_matches,
    enrich_candidates,
    extract_name_query,
    positions_in,
    resolve_by_name,
    resolve_indices,
    resolve_one,
)
from agent.turn_args import quantity_in, size_step, spec_change_cue
from i18n import format_money, is_english, t
from store.cart_store import CartStockError
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


_LEAVE_QUESTION_RE = re.compile(r"下单|结算|看看?购物车|购物车里|删掉|去掉")


def detect_order_intent(text: str) -> str | None:
    """返回 "history"、"reorder" 或 None。"""
    text = (text or "").strip()
    if not text or is_english():  # The English router reports these as cart actions.
        return None
    if _ORDER_HISTORY_RE.search(text):
        return "history"
    if any(word in text for word in _REORDER_WORDS) and not any(word in text for word in _NOT_REORDER_WORDS):
        return "reorder"
    return None


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
        name="change_spec",
        description="把购物车里某件商品换成别的规格（'换成 L 码'/'改成黑色'/'大一码'）",
        parameters={
            "cart_index": {"type": "integer", "description": "购物车里第几件，从 1 开始；说'刚加的/这件'时省略。"},
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

        try:
            if "cart_action" in (slots or {}):
                return self._run_routed(raw_query, session, slots)
            if self._leaves_question(raw_query):
                session.task.end_pending("cart_sku", "cart_pick")
            # A waiting question takes this turn as its answer.
            pending = session.task.pending_data("cart_sku")
            if pending:
                return self._resolve_pending_sku(raw_query, session, pending)
            pending_add = session.task.pending_data("cart_pick")
            if pending_add:
                return self._resolve_pending_add(raw_query, session, pending_add)

            order_intent = detect_order_intent(raw_query)
            if order_intent:
                action, args = {"history": "order_history", "reorder": "reorder"}[order_intent], {}
            elif self.is_spec_change(raw_query, session):
                action, args = "change_spec", {}
            else:
                action, args = self._decide(query, session)
            logger.info("cart: action=%s args=%s", action, args)
            return self._perform(action, raw_query, session, args, slots)
        except CartStockError as exc:
            return self._plain(str(exc))
        except CartNotFoundError as exc:
            return self._plain(t("cart.error", detail=str(exc)))

    def _run_routed(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        """Carry out the action the router read from the message."""
        action = slots["cart_action"]
        if action == "cancel":
            session.task.end_pending("cart_sku", "cart_pick")
            return self._plain(t("cart.cancelled"))
        if action == "answer":
            pending = session.task.pending_data("cart_sku")
            if pending:
                return self._resolve_pending_sku(query, session, pending, (slots.get("spec_update") or {}).get("step", 0))
            pending_add = session.task.pending_data("cart_pick")
            if pending_add:
                return self._resolve_pending_add(query, session, pending_add)
            action = "add"  # Nothing was waiting, so the answer names a product to add.
        session.task.end_pending("cart_sku", "cart_pick")
        args = {"cart_index": slots.get("cart_item"), "quantity": slots.get("quantity")}
        logger.info("cart: routed action=%s args=%s", action, args)
        return self._perform(action, query, session, args, slots)

    def _perform(
        self, action: str, query: str, session: AgentSession, args: dict[str, Any], slots: dict[str, Any],
    ) -> ToolResult:
        if action == "add":
            return self._add(query, session, args, slots)
        if action == "change_spec":
            return self._change_spec(query, session, args, slots)
        if action == "set_quantity":
            return self._set_quantity(args)
        if action == "remove":
            return self._remove(args)
        if action == "checkout":
            return self._checkout(args, session)
        if action == "reorder":
            return self._reorder(query, session, slots)
        if action == "order_history":
            return self._order_history(session)
        return self._view(session)  # view 及未知动作的安全兜底

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

    @staticmethod
    def _leaves_question(query: str) -> bool:
        """Whether the shopper moved on to checkout, the cart or their orders (Chinese rules)."""
        if is_english():
            return False  # The English router ends the question when the shopper moves on.
        return bool(detect_order_intent(query)) or _LEAVE_QUESTION_RE.search(query) is not None

    def is_spec_change(self, query: str, session: AgentSession) -> bool:
        """Whether this message changes an option of the item just added (Chinese rules)."""
        added = session.task.last_added
        if is_english() or not added or not spec_change_cue(query):
            return False
        return bool(size_step(query) or sku_select.mentioned_constraints(query, self._store.list_skus(added["product_id"])))

    def _add(
        self, query: str, session: AgentSession, args: dict[str, Any], slots: dict[str, Any] | None = None
    ) -> ToolResult:
        """Resolve the product and begin the add-to-cart flow.

        Priority is explicit ordinal, current-list name, catalog lookup, focused product,
        then a sole candidate. Ambiguous candidates trigger clarification; an unrelated
        product is never selected silently.
        """
        slots = slots or {}
        last_hits = session.task.add_targets()
        qty = slots.get("quantity") or _as_int(args.get("quantity"), default=1)

        # An explicitly named brand in the raw utterance is more reliable than an index
        # inferred from rewritten context, so calculate brand matches first.
        brand_hits = self._products.match_brands_in_text(query)
        brand_ids = {c.product_id for c in brand_hits}

        # 1) 显式序号：只采信用户原句里真的出现的"第N个/最后一个"。
        # dispatch_action 的 args.index 可能来自 router/LLM 对上下文的猜测；
        # 用户只说"帮我加入购物车"时若上一轮刚聚焦了小米，不能让这个猜测
        # 覆盖 last_focus_product_id，错加成列表第一个 iPad。
        parsed = positions_in(query, last_hits, session, slots)
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
            return self._ask_which_product(subset, session, qty)

        # 2.5) 没点名、只说了个规格值（「42 码的加进来」「黑色的来一件」）：
        #      指向刚聚焦（详情介绍过 / 刚加购过）的那一款，规格交给 _begin_add 用同一句话选。
        focus = session.task.focus_product_id
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
            session.task.show("candidates", hits)
            return self._ask_which_product(hits, session, qty)

        # 4. Use the focused product for an unnamed demonstrative request.
        focus = session.task.focus_product_id
        if focus and not keyword:
            return self._begin_add(focus, qty, session, query, source="focus")

        # 5. A named but missing product must not fall back to an unrelated visible item.
        if keyword or brand_ids:
            return self._plain(t("cart.no_match", name=keyword or query))

        # 6. Default only when a single candidate exists; otherwise clarify.
        if len(last_hits) == 1:
            return self._begin_add(last_hits[0]["product_id"], qty, session, query, source="only_candidate")
        if last_hits:
            return self._ask_which_product(last_hits, session, qty)

        return self._plain(t("cart.ask_add"))

    def _ask_which_product(
        self, hits: list[dict[str, Any]], session: AgentSession, quantity: int = 1
    ) -> ToolResult:
        """List candidates when reference resolution cannot identify one product."""
        candidates = [
            {"product_id": h["product_id"], "title": h.get("title", "")} for h in hits
        ]
        session.task.ask("cart_pick", {"candidates": candidates, "quantity": quantity})
        lines = [f"{i + 1}. {c['title']}" for i, c in enumerate(candidates)]
        text = t("cart.which_product", lines="\n".join(lines))
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
            session.task.end_pending()
            return self._plain(t("cart.cancelled"))

        candidates: list[dict[str, Any]] = pending_add.get("candidates", [])
        if not candidates:
            session.task.end_pending()
            return self._plain(t("cart.no_candidates"))

        if pending_add.get("reorder"):
            picked = resolve_one(query, _reorder_candidates(candidates))
            if picked is None:
                return self._ask_which_reorder(candidates, session, pending_add.get("quantity"))
            quantity = quantity_in(query) or pending_add.get("quantity")
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
            return self._ask_which_product(candidates, session, _as_int(pending_add.get("quantity"), default=1))

        session.task.end_pending()
        quantity = quantity_in(query) or _as_int(pending_add.get("quantity"), default=1)
        return self._begin_add(candidates[picked]["product_id"], quantity, session, query, source="pending_add")

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
        session.task.end_pending()
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
            return self._plain(t("cart.no_sku"))

        if len(skus) == 1:
            return self._commit_add(product_id, skus[0]["sku_id"], qty, session, resolution=resolution)

        # Check whether the current utterance already identifies one SKU.
        selected = sku_select.mentioned_constraints(query, skus)
        matched = sku_select.matching(skus, selected)
        if len(matched) == 1:
            return self._commit_add(product_id, matched[0]["sku_id"], qty, session, resolution=resolution)

        title = self._store.product_title(product_id)
        session.task.ask("cart_sku", {
            "product_id": product_id,
            "title": title,
            "quantity": qty,
            "selected": selected,
            "resolution": resolution,
        })
        return self._ask_spec(product_id, title, matched if matched else skus, session, resolution=resolution)

    def _build_resolution(self, product_id: str, query: str, source: str) -> dict[str, Any]:
        """Record the resolved product for diagnostics and confirmation copy."""
        title = self._store.product_title(product_id)
        return {
            "original_query": query,
            "resolved_query": t("cart.resolved_query", title=title),
            "product_id": product_id,
            "title": title,
            "source": source,
        }

    def _resolve_pending_sku(
        self, query: str, session: AgentSession, pending: dict[str, Any], step: int | None = None
    ) -> ToolResult:
        """Resolve a pending SKU answer and add it, or continue clarification."""
        if self._is_cancel(query):
            session.task.end_pending()
            return self._plain(t("cart.cancelled"))

        # Restart product resolution when another brand is named during SKU selection.
        switched = self._switch_intent(query, session, [pending["product_id"]])
        if switched is not None:
            return switched

        product_id = pending["product_id"]
        qty = quantity_in(query) or _as_int(pending.get("quantity"), default=1)
        skus = self._store.list_skus(product_id)
        if not skus:
            session.task.end_pending()
            return self._plain(t("cart.sku_lost"))

        # Each answer sets only the fields it names, so "actually large" replaces "medium".
        earlier = pending.get("selected") or {}
        named = sku_select.mentioned_constraints(query, skus) or sku_select.stepped_size(skus, earlier, size_step(query) if step is None else step)
        selected = {**earlier, **named}
        matched = sku_select.matching(skus, selected)
        if not matched:  # The new choice conflicts with an earlier one; the latest wins.
            selected = named
            matched = sku_select.matching(skus, selected)
        if len(matched) != 1:
            picked = self._match_ordinal(query, matched if matched else skus)
            if picked is not None:
                matched = [picked]

        pending = {**pending, "selected": selected, "quantity": qty}
        session.task.ask("cart_sku", pending)  # Stays open until the cart write succeeds.
        if len(matched) == 1:
            return self._commit_add(
                product_id,
                matched[0]["sku_id"],
                qty,
                session,
                resolution=pending.get("resolution"),
            )
        return self._ask_spec(
            product_id,
            pending["title"],
            matched if matched else skus,
            session,
            resolution=pending.get("resolution"),
        )

    def _commit_add(
        self,
        product_id: str,
        sku_id: str,
        qty: int,
        session: AgentSession,
        resolution: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Persist a resolved SKU and return the confirmation result.

        A failed write leaves any waiting question open, so the shopper can pick again.
        """
        try:
            line = self._store.add_product(product_id, sku_id=sku_id, quantity=qty)
        except CartStockError as exc:
            return self._plain(str(exc))
        session.task.added_to_cart(product_id, sku_id)
        snapshot = self._snapshot()
        text = t(
            "cart.added", title=line.title, spec=_options_suffix(line.options), quantity=line.quantity,
            count=snapshot["cart"]["item_count"], total=snapshot["cart"]["total"],
            money=format_money(snapshot["cart"]["total"]),
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
        session: AgentSession,
        resolution: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Ask for varying SKU dimensions instead of listing every combination."""
        dims = self._varying_dims(skus)
        if not dims:
            # Defensive fallback for multiple SKUs with no varying dimension.
            return self._commit_add(product_id, skus[0]["sku_id"], 1, session)

        # Let the client render options; keep narrative copy concise.
        text = t("cart.ask_spec", title=title)
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
        # The English router reports a cancel as its own action.
        return not is_english() and any(word in query for word in _CANCEL_WORDS)

    def _change_spec(
        self, query: str, session: AgentSession, args: dict[str, Any], slots: dict[str, Any]
    ) -> ToolResult:
        """Change the named options of a cart line and keep the rest."""
        line = self._line_to_change(args.get("cart_index"), session)
        if line is None:
            return self._plain(t("cart.ask_which_line"))
        skus = self._store.list_skus(line.product_id)
        step = (slots.get("spec_update") or {}).get("step", 0) if "cart_action" in slots else size_step(query)
        changes = sku_select.spec_changes(query, skus, line.options, step)
        options = _join([str(value) for values in sku_select.varying_dims(skus).values() for value in values])
        if not changes:
            if args.get("quantity") is not None:  # "Change it to 2" on a product without a size 2.
                return self._set_quantity(args)
            return self._plain(t("cart.spec_not_found", title=line.title, options=options))
        wanted = sku_select.matching(skus, {**line.options, **changes})
        if not wanted:
            return self._plain(t("cart.spec_unavailable", title=line.title, spec=_join(list(changes.values())), options=options))
        updated = self._store.change_sku(line.cart_item_id, wanted[0]["sku_id"])
        session.task.added_to_cart(updated.product_id, updated.sku_id)
        snapshot = self._snapshot()
        total = snapshot["cart"]["total"]
        return ToolResult(
            tool_name=self.name,
            payload={"action": "change_spec", "changed": updated.to_dict(), **snapshot},
            narrative_override=t(
                "cart.spec_changed", title=updated.title, spec=_options_suffix(updated.options), total=total, money=format_money(total),
            ),
            needs_composer=False,
        )

    def _line_to_change(self, cart_index: Any, session: AgentSession) -> Any:
        """The cart line meant: a stated position, the item just added, the focus, or the only line."""
        lines = self._store.list_items()
        index = _as_int(cart_index, default=None)
        if index is not None:
            return lines[index - 1] if 0 < index <= len(lines) else None
        added = session.task.last_added or {}
        for line in lines:
            if line.sku_id == added.get("sku_id"):
                return line
        focused = [line for line in lines if line.product_id == session.task.focus_product_id]
        if len(focused) == 1:
            return focused[0]
        return lines[0] if len(lines) == 1 else None

    def _set_quantity(self, args: dict[str, Any]) -> ToolResult:
        line = self._target_line(args.get("cart_index"))
        qty = _as_int(args.get("quantity"), default=None)
        if qty is None:
            return self._plain(t("cart.ask_quantity"))
        updated = self._store.set_quantity(line.cart_item_id, qty)
        snapshot = self._snapshot()
        total = snapshot["cart"]["total"]
        if updated is None:
            hint = t("cart.quantity_removed", title=line.title)
        else:
            hint = t("cart.quantity_set", title=updated.title, quantity=updated.quantity)
        return self._reported({"action": "set_quantity", **snapshot}, hint + t("cart.confirm_total", total=total, money=format_money(total)))

    def _remove(self, args: dict[str, Any]) -> ToolResult:
        line = self._target_line(args.get("cart_index"))
        self._store.remove_item(line.cart_item_id)
        snapshot = self._snapshot()
        total = snapshot["cart"]["total"]
        return self._reported(
            {"action": "remove", "removed": line.to_dict(), **snapshot},
            t("cart.removed", title=line.title, count=snapshot["cart"]["item_count"], total=total, money=format_money(total)),
        )

    def _view(self, session: AgentSession) -> ToolResult:
        snapshot = self._snapshot()
        if not snapshot["cart"]["lines"]:
            return self._plain(t("cart.empty"))
        session.task.show("cart", [{"product_id": line["product_id"], "title": line["title"]} for line in snapshot["cart"]["lines"]])
        total = snapshot["cart"]["total"]
        rows = "\n".join(
            f"{i + 1}. {line['title'][:60]} × {line['quantity']} — {format_money(line['unit_price'] * line['quantity'])}"
            for i, line in enumerate(snapshot["cart"]["lines"])
        ) if is_english() else ""
        return self._reported(
            {"action": "view", **snapshot},
            t("cart.view", rows=rows, total=total, money=format_money(total)),
        )

    def _checkout(self, args: dict[str, Any], session: AgentSession) -> ToolResult:
        address = args.get("address") or DEFAULT_ADDRESS
        try:
            order = self._store.build_order(address=address, order_user_id=self._order_user(session))
        except CartStockError as exc:
            return self._plain(str(exc))
        return self._reported(
            {"action": "checkout", "order": order.to_dict()},
            t("cart.ordered", order_id=order.order_id, count=order.to_dict()["item_count"], total=order.total,
              money=format_money(order.total), address=order.address),
        )

    # ------------------------------ 买过的东西 ------------------------------

    @staticmethod
    def _order_user(session: AgentSession) -> str:
        return session.user_id or DEMO_USER_ID

    def _reorder(self, query: str, session: AgentSession, slots: dict[str, Any] | None = None) -> ToolResult:
        """把买过的东西加回购物车。不直接下单：现价可能变了，要用户再确认一次。"""
        user_id = self._order_user(session)
        orders = self._orders.list_orders(user_id)
        if not orders:
            return self._plain(t("cart.no_orders"))

        if slots is not None and "cart_action" in slots:  # The router already read the message.
            quantity, wants_whole_order, named = slots.get("quantity"), bool(slots.get("whole_order")), slots.get("reorder_item") or ""
        else:
            quantity = quantity_in(query)
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
            picked = resolve_one(named or query, _reorder_candidates(candidates))
            matched = [candidates[picked]] if picked is not None else []
        if matched:
            return self._commit_reorder(matched[0], quantity, session)
        titles = _join([line["title"][:30 if is_english() else 14] for line in candidates[:3]])
        return self._plain(t("cart.reorder_not_found", name=named, titles=titles))

    def _ask_which_reorder(
        self,
        lines: list[dict[str, Any]],
        session: AgentSession,
        quantity: int | None = None,
    ) -> ToolResult:
        candidates = lines[:5]
        session.task.ask("cart_pick", {"candidates": candidates, "reorder": True, "quantity": quantity})
        rows = [
            t("cart.reorder_row", index=i + 1, title=line["title"][:40 if is_english() else 24],
              spec=_spec_suffix(line), price=format_money(line["unit_price"]))
            for i, line in enumerate(candidates)
        ]
        return ToolResult(
            tool_name=self.name,
            payload={"action": "ask_which_product", "candidates": candidates},
            narrative_override=t("cart.which_reorder", rows="\n".join(rows)),
            needs_composer=False,
        )

    def _price_note(self, previous: float, current: float) -> str:
        if abs(previous - current) < 0.01:
            return t("cart.price_same", price=format_money(current))
        key = "cart.price_lower" if current < previous else "cart.price_higher"
        return t(key, price=format_money(current), diff=format_money(abs(current - previous)))

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
        except (CartNotFoundError, CartStockError):
            # 上次的规格下架了：商品还在就让用户重新选规格，不擅自换成别的规格
            if self._store.list_skus(product_id):
                result = self._begin_add(product_id, qty, session, "", source="reorder")
                prefix = t("cart.reorder_spec_gone", spec=_spec_suffix(line) or t("cart.spec_word"))
                return dataclasses.replace(result, narrative_override=prefix + (result.narrative_override or ""))
            return self._plain(t("cart.reorder_delisted", title=line["title"][:40 if is_english() else 20]))
        session.task.added_to_cart(product_id, added.sku_id)
        snapshot = self._snapshot()
        current = next(
            (s["price"] for s in self._store.list_skus(product_id) if s["sku_id"] == line["sku_id"]),
            added.unit_price,
        )
        text = t(
            "cart.reordered", title=line["title"][:40 if is_english() else 24], spec=_spec_suffix(line), quantity=qty,
            note=self._price_note(float(line["unit_price"]), float(current)),
            total=f"{snapshot['cart']['total']:g}", money=format_money(snapshot["cart"]["total"]),
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
            except (CartNotFoundError, CartStockError):
                skipped.append(line["title"][:16])
        if not added:
            return self._plain(t("cart.whole_order_gone"))
        snapshot = self._snapshot()
        previous = round(sum(float(l["unit_price"]) * int(l["quantity"]) for l in order["lines"]), 2)
        text = t(
            "cart.whole_order_added", count=len(added), total=f"{snapshot['cart']['total']:g}",
            money=format_money(snapshot["cart"]["total"]), previous=f"{previous:g}", previous_money=format_money(previous),
        )
        if skipped:
            text += t("cart.whole_order_skipped", titles=_join(skipped))
        return ToolResult(
            tool_name=self.name,
            payload={"action": "reorder", "added_lines": added, **snapshot},
            narrative_override=text + t("cart.ask_checkout"),
            needs_composer=False,
        )

    def _order_history(self, session: AgentSession) -> ToolResult:
        orders = self._orders.list_orders(self._order_user(session), limit=5)
        if not orders:
            return self._plain(t("cart.no_history"))
        rows: list[str] = []
        hits: list[dict[str, str]] = []
        for order in orders:
            names = _join([f"{line['title'][:40 if is_english() else 14]}×{line['quantity']}" for line in order["lines"][:3]])
            more = t("cart.history_more", count=len(order["lines"])) if len(order["lines"]) > 3 else ""
            rows.append(t("cart.history_row", date=order["created_at"][:10], names=names, more=more, total=format_money(order["total"])))
            for line in order["lines"]:
                if all(hit["product_id"] != line["product_id"] for hit in hits):
                    hits.append({"product_id": line["product_id"], "title": line["title"]})
        # 接下来说「第一个详细说说」「再买第二个」能指到这些商品
        session.task.show("orders", hits)
        return ToolResult(
            tool_name=self.name,
            payload={"action": "order_history", "orders": orders},
            narrative_override=t("cart.history", rows="\n".join(rows)),
            needs_composer=False,
        )

    # ------------------------------ 辅助 ------------------------------

    def _target_line(self, cart_index: Any):
        """Resolve a cart ordinal to a line, defaulting to the first line."""
        lines = self._store.list_items()
        if not lines:
            raise CartNotFoundError(t("cart.error.empty"))
        i = (int(cart_index) - 1) if cart_index is not None else 0
        if not (0 <= i < len(lines)):
            raise CartNotFoundError(t("cart.error.no_index", index=cart_index))
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

    def _reported(self, payload: dict[str, Any], text: str) -> ToolResult:
        """Deterministic English copy, or a hint the Chinese composer turns into prose."""
        if is_english():
            return ToolResult(tool_name=self.name, payload=payload, narrative_override=text, needs_composer=False)
        return ToolResult(tool_name=self.name, payload=payload, composer_hint=text)

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
    return t("cart.spec_suffix", spec=_join([str(v) for v in options.values()])) if options else ""


def _options_suffix(options: dict[str, Any]) -> str:
    if not options:
        return ""
    if is_english():
        return t("cart.spec_suffix", spec=", ".join(f"{k}: {v}" for k, v in options.items()))
    return t("cart.spec_suffix", spec="、".join(f"{k}{v}" for k, v in options.items()))


def _join(parts: list[str]) -> str:
    return (", " if is_english() else "、").join(parts)


def _as_int(value: Any, default: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
