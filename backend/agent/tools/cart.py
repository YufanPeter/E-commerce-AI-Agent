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

import logging
from collections import OrderedDict
import re
from typing import Any

from agent.llm_actions import ActionSpec, dispatch_action
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.resolve import resolve_one
from agent.tools.reference import resolve_indices, resolve_by_name, extract_name_query
from store.cart_store import CartNotFoundError, CartStore, DEFAULT_ADDRESS
from store.product_store import ProductStore


logger = logging.getLogger(__name__)

# Cancellation terms that clear pending SKU selection without adding an item.
_CANCEL_WORDS = ("算了", "不加了", "不要了", "取消", "先不", "不用了", "不买了")


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
]

_PURPOSE = "管理用户的购物车（加购、改数量、删除、查看）并完成下单结算"


class CartTool:
    name: str = "cart"

    def __init__(
        self,
        cart_store: CartStore | None = None,
        product_store: ProductStore | None = None,
    ) -> None:
        self._store = cart_store or CartStore()
        # Catalog-wide lookup handles named products absent from the current results.
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
                return self._checkout(args)
            return self._view()  # Safe fallback for view and unknown actions.
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

        # 1. Trust only ordinals explicitly present in the raw utterance.
        parsed = resolve_indices(query, len(last_hits))
        idx = (parsed[0] + 1) if parsed else None
        if idx is not None:
            i = int(idx) - 1
            if 0 <= i < len(last_hits):
                target_id = last_hits[i]["product_id"]
                # Accept the index only when it does not conflict with an explicit brand.
                if not brand_ids or target_id in brand_ids:
                    return self._begin_add(target_id, qty, session, query, source="explicit_index")

        # 2. Match a name or brand in the current recommendation list.
        named = resolve_by_name(query, last_hits)
        if len(named) == 1:
            return self._begin_add(last_hits[named[0]]["product_id"], qty, session, query, source="context_name")
        if len(named) > 1:
            # Semantically resolve same-brand matches across categories before clarifying.
            subset = [last_hits[i] for i in named]
            picked = resolve_one(query, self._enrich_candidates(subset))
            if picked is not None:
                return self._begin_add(subset[picked]["product_id"], qty, session, query, source="semantic_context_match")
            return self._ask_which_product(subset, session)

        # 3. Search the full catalog: direct brand substring first, then extracted
        # model/category keywords against titles.
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

        Enrichment failures do not block title-only resolution.
        """
        enriched: list[dict[str, Any]] = []
        for cand in candidates:
            item = dict(cand)
            try:
                detail = self._products.get_product_detail(cand["product_id"])
            except Exception:  # noqa: BLE001
                detail = None
            if detail is not None:
                item.setdefault("title", detail.title)
                item["brand"] = detail.brand
                item["category"] = detail.category
                item["sub_category"] = detail.sub_category
            enriched.append(item)
        return enriched

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

        # Restart resolution when the user names a product outside the pending candidates.
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
        """Return dimensions with more than one value in the given SKU set.

        Numeric values are sorted ascending; other dimensions preserve appearance order.
        """
        dim_values: "OrderedDict[str, list[str]]" = OrderedDict()
        for sku in skus:
            for key, val in sku["options"].items():
                dim_values.setdefault(key, [])
                if val not in dim_values[key]:
                    dim_values[key].append(val)
        return OrderedDict(
            (k, CartTool._sort_values(vs)) for k, vs in dim_values.items() if len(vs) > 1
        )

    @staticmethod
    def _sort_values(values: list[str]) -> list[str]:
        """Sort numerically when every value contains a number; otherwise preserve order."""
        keyed: list[tuple[int, str]] = []
        for v in values:
            match = re.search(r"\d+", v)
            if not match:
                return values
            keyed.append((int(match.group()), v))
        return [v for _, v in sorted(keyed, key=lambda x: x[0])]

    def _filter_skus(
        self, query: str, skus: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter SKUs by specification values recognized in the user utterance.

        No recognized value returns the original set and triggers clarification. Values
        across dimensions are intersected so every returned SKU satisfies all constraints.

        Matching is deterministic first: exact canonical values win, with the longest
        match breaking shared-prefix cases. A unique token-score winner is used only when
        exact matching fails; ties leave the dimension unconstrained.
        """
        dims = self._varying_dims(skus)
        constraints: dict[str, str] = {}
        for dim, values in dims.items():
            chosen = self._pick_dim_value(values, query)
            if chosen is not None:
                constraints[dim] = chosen
        if not constraints:
            return list(skus)
        return [
            sku for sku in skus
            if all(sku["options"].get(d) == v for d, v in constraints.items())
        ]

    @staticmethod
    def _pick_dim_value(values: list[str], query: str) -> str | None:
        """Select the dimension value named by the user, or return ``None``."""
        q = query or ""
        q_lower = q.lower()
        # Exact case-insensitive match; prefer the longest to avoid substring collisions.
        exact = [
            v for v in values
            if v and (v in q or v.lower() in q_lower)
        ]
        if exact:
            exact.sort(key=len, reverse=True)
            # Accept one longest match; leave a theoretical tie unconstrained.
            if len(exact) == 1 or len(exact[0]) > len(exact[1]):
                return exact[0]
            return None
        # Fuzzy fallback accepts a unique highest score; ties remain ambiguous.
        best_val: str | None = None
        best_score = 0
        tie = False
        for val in values:
            score = CartTool._value_match_score(val, q)
            if score > best_score:
                best_score, best_val, tie = score, val, False
            elif score == best_score and score > 0:
                tie = True
        if best_val is not None and best_score > 0 and not tie:
            return best_val
        return None

    @staticmethod
    def _value_match_score(value: str, query: str) -> int:
        """Score specification-value tokens matched in the user utterance.

        Higher scores indicate more complete matches within one dimension.

        Whole-token matching avoids loose substring collisions. Numbers require numeric
        boundaries, Chinese runs use meaningful substrings, and Latin words require at
        least three letters.
        """
        value = (value or "").strip()
        if not value:
            return 0
        q = query.lower()
        score = 0
        # Letter sizes use boundaries so L does not match XL and S does not match XS.
        size_match = re.fullmatch(r"([A-Za-z]{1,3})(码)?", value)
        if size_match:
            token = value.lower()
            right_guard = "" if size_match.group(2) else r"(?![a-z])"
            if re.search(rf"(?<![a-z]){re.escape(token)}{right_guard}", q):
                score += 1
            return score
        # Numeric tokens require boundaries so 28 does not match 280.
        for num in re.findall(r"\d+", value):
            if re.search(rf"(?<!\d){re.escape(num)}(?!\d)", query):
                score += 1
        # Chinese runs require at least two characters; longer values allow bigrams.
        for run in re.findall(r"[\u4e00-\u9fff]+", value):
            if len(run) >= 2 and run in query:
                score += 1
            elif len(run) >= 3:
                for i in range(len(run) - 1):
                    if run[i:i + 2] in query:
                        score += 1
                        break
        # Latin words require at least three letters.
        for word in re.findall(r"[a-zA-Z]{3,}", value):
            if word.lower() in q:
                score += 1
        return score


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

    def _checkout(self, args: dict[str, Any]) -> ToolResult:
        address = args.get("address") or DEFAULT_ADDRESS
        order = self._store.build_order(address=address)
        return ToolResult(
            tool_name=self.name,
            payload={"action": "checkout", "order": order.to_dict()},
            composer_hint=(
                f"下单成功！订单号 {order.order_id}，共 {order.to_dict()['item_count']} 件、"
                f"合计 {order.total} 元，寄往：{order.address}。"
                "请用愉快的口吻确认下单完成。"
            ),
        )

    # ------------------------------ Helpers ------------------------------

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


def _as_int(value: Any, default: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
