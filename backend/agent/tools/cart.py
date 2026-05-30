from __future__ import annotations

"""CartTool：对话式购物车与下单（"把刚才那款加进来"/"删掉第二个"/"下单吧"）。

这是"高级"档需求：对话式 CRUD + 多轮状态管理 + 业务闭环。

架构：
    intent_router 把购物相关的"管车/下单"意图粗分到 cart，CartTool 再用
    **统一工具调用层**（agent.llm_actions.dispatch_action）把这句话细分到
    具体动作（add/remove/set_quantity/view/checkout）并抽参数。
    动作选定后，用确定性代码操作 CartStore（SQLite），副作用可控可测。

指代消解复用 reference 模块：
    - "把刚才那款/这个加进来" → last_focus_product_id（product_detail 写的）
      或 last_hits[0]
    - "删掉第二个" → 购物车当前明细的第 2 行（注意：是购物车的顺序，
      不是推荐列表的顺序）

降级：LLM 动作分发失败时，退回一个安全默认——展示当前购物车，
      让用户看到状态而不是报错。
"""

import logging
from typing import Any

from agent.llm_actions import ActionSpec, dispatch_action
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.reference import resolve_indices
from store.cart_store import CartNotFoundError, CartStore, DEFAULT_ADDRESS


logger = logging.getLogger(__name__)


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

    def __init__(self, cart_store: CartStore | None = None) -> None:
        self._store = cart_store or CartStore()

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        action, args = self._decide(query, session)
        logger.info("cart: action=%s args=%s", action, args)
        try:
            if action == "add":
                return self._add(query, session, args)
            if action == "set_quantity":
                return self._set_quantity(args)
            if action == "remove":
                return self._remove(args)
            if action == "checkout":
                return self._checkout(args)
            return self._view()  # view 及未知动作的安全兜底
        except CartNotFoundError as exc:
            return self._plain(str(exc) + "。要不先看看购物车或换一件？")

    # ------------------------------ 动作分发 ------------------------------

    def _decide(self, query: str, session: AgentSession) -> tuple[str, dict[str, Any]]:
        """用统一工具调用层选动作；LLM 失败时退回 view（展示状态而非报错）。"""
        try:
            decision = dispatch_action(query, session, _ACTIONS, purpose=_PURPOSE)
            return decision.action, decision.args
        except Exception as exc:  # noqa: BLE001
            logger.warning("cart action dispatch failed, fallback to view: %r", exc)
            return "view", {}

    # ------------------------------ 各动作 ------------------------------

    def _add(self, query: str, session: AgentSession, args: dict[str, Any]) -> ToolResult:
        product_id = self._resolve_add_target(query, session, args)
        if product_id is None:
            return self._plain(
                "想把哪款加进来呀？可以说「把第一个加入购物车」，"
                "或者先让我推荐几款。"
            )
        qty = _as_int(args.get("quantity"), default=1)
        line = self._store.add_product(product_id, quantity=qty)
        snapshot = self._snapshot()
        return ToolResult(
            tool_name=self.name,
            payload={"action": "add", "added": line.to_dict(), **snapshot},
            composer_hint=(
                f"已把「{line.title}」加入购物车（数量 {line.quantity}）。"
                f"请确认加购成功，并自然带出购物车当前共 {snapshot['cart']['item_count']} 件、"
                f"合计 {snapshot['cart']['total']} 元。"
            ),
        )

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

    # ------------------------------ 辅助 ------------------------------

    def _resolve_add_target(
        self, query: str, session: AgentSession, args: dict[str, Any]
    ) -> str | None:
        """定位要加车的 product_id。

        优先级：显式序号(index) → 上一轮 detail 聚焦的商品 → last_hits 首个。
        """
        last_hits = session.recall_hits()
        idx = args.get("index")
        if idx is None:
            parsed = resolve_indices(query, len(last_hits))
            idx = (parsed[0] + 1) if parsed else None
        if idx is not None:
            i = int(idx) - 1
            if 0 <= i < len(last_hits):
                return last_hits[i]["product_id"]
        focus = session.get("last_focus_product_id")
        if focus:
            return focus
        if last_hits:
            return last_hits[0]["product_id"]
        return None

    def _target_line(self, cart_index: Any):
        """把"购物车第 N 件"定位到具体 CartLine；缺省取第一件。"""
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
