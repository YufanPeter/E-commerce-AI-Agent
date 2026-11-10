from __future__ import annotations

"""购物车 SQLite 访问层。

承接 init.sql 里已建好的 cart_items 表，提供对话式购物车需要的 CRUD：
    - add_item / set_quantity / remove_item / clear
    - list_items（带商品标题、SKU 规格、单价、小计）
    - build_order（从勾选项算订单汇总，下单后 clear）

设计取舍：
    - 演示期单用户：默认 user_id = 'demo_user'（init.sql 已 seed）。
      多用户时把 user_id 换成会话/登录态即可，接口已留参数。
    - 下单写入 orders / order_lines（见 store.order_store），和清空购物车在同一事务里。
      暂不做支付和物流状态机。
    - 加车默认选最便宜的在售 SKU——对话里用户很少指定规格，
      "把这款加进来"应当能直接成交，需要换规格再说"要XX色的"。
"""

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from i18n import t
from store.order_store import insert_order
from store.product_store import DEFAULT_DB_PATH, load_json_text


DEMO_USER_ID = "demo_user"
DEFAULT_ADDRESS = t("cart.default_address")


@dataclass(frozen=True)
class CartLine:
    """One cart line representing one SKU."""

    cart_item_id: int
    product_id: str
    sku_id: str
    title: str
    options: dict[str, Any]
    quantity: int
    unit_price: float
    selected: bool

    @property
    def subtotal(self) -> float:
        return round(self.unit_price * self.quantity, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cart_item_id": self.cart_item_id,
            "product_id": self.product_id,
            "sku_id": self.sku_id,
            "title": self.title,
            "options": self.options,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "subtotal": self.subtotal,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class OrderSummary:
    """Structured result of one checkout."""

    order_id: str
    address: str
    lines: list[CartLine]
    total: float
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "created_at": self.created_at,
            "address": self.address,
            "lines": [line.to_dict() for line in self.lines],
            "item_count": sum(line.quantity for line in self.lines),
            "total": self.total,
        }


class CartStockError(Exception):
    """The requested SKU cannot supply the requested quantity."""


class CartNotFoundError(Exception):
    """Raised for a missing product or cart line and translated by the tool."""


class CartStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    # ------------------------------ Read ------------------------------

    def list_items(self, user_id: str = DEMO_USER_ID) -> list[CartLine]:
        """Return cart lines in insertion order with joined product titles."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.cart_item_id, c.product_id, c.sku_id, c.selected_options_json,
                       c.quantity, c.unit_price, c.selected, p.title
                FROM cart_items c
                JOIN products p ON p.product_id = c.product_id
                WHERE c.user_id = ?
                ORDER BY c.cart_item_id ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._line_from_row(row) for row in rows]

    def list_skus(self, product_id: str) -> list[dict[str, Any]]:
        """List active SKUs and prices for one product in ascending price order.

        More than one result tells ``CartTool`` to request a specification selection.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sku_id, properties_json, price, stock_qty FROM product_skus "
                "WHERE product_id = ? AND status = 'active' "
                "ORDER BY price ASC, sku_id ASC",
                (product_id,),
            ).fetchall()
        return [
            {
                "sku_id": row["sku_id"],
                "options": load_json_text(row["properties_json"]),
                "price": float(row["price"]),
                "stock_qty": int(row["stock_qty"]),
            }
            for row in rows
        ]

    def product_title(self, product_id: str) -> str:
        """Return a product title, falling back to its ID when absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT title FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()
        return row["title"] if row else product_id

    # ------------------------------ Write ------------------------------

    def add_product(
        self,
        product_id: str,
        user_id: str = DEMO_USER_ID,
        sku_id: str | None = None,
        quantity: int = 1,
    ) -> CartLine:
        """Add a product to the cart.

        When ``sku_id`` is absent, select the cheapest active SKU. Existing lines add
        quantity through the unique constraint and UPSERT.
        """
        if quantity <= 0:
            quantity = 1
        with self._connect() as conn:
            sku = self._resolve_sku(conn, product_id, sku_id)
            if sku is None:
                raise CartNotFoundError(t("cart.error.no_sku", product_id=product_id))
            self._require_stock(conn, user_id, sku, quantity)
            conn.execute(
                """
                INSERT INTO cart_items
                    (user_id, product_id, sku_id, selected_options_json,
                     quantity, unit_price, selected)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id, sku_id) DO UPDATE SET
                    quantity = quantity + excluded.quantity,
                    unit_price = excluded.unit_price,
                    selected = 1
                """,
                (user_id, product_id, sku["sku_id"], sku["properties_json"],
                 quantity, float(sku["price"])),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT c.cart_item_id, c.product_id, c.sku_id, c.selected_options_json,
                       c.quantity, c.unit_price, c.selected, p.title
                FROM cart_items c JOIN products p ON p.product_id = c.product_id
                WHERE c.user_id = ? AND c.sku_id = ?
                """,
                (user_id, sku["sku_id"]),
            ).fetchone()
        return self._line_from_row(row)

    def set_quantity(
        self, cart_item_id: int, quantity: int, user_id: str = DEMO_USER_ID
    ) -> CartLine | None:
        """Set line quantity; non-positive values remove the line and return ``None``."""
        if quantity <= 0:
            self.remove_item(cart_item_id, user_id)
            return None
        with self._connect() as conn:
            stock = conn.execute(
                """
                SELECT s.stock_qty FROM cart_items c JOIN product_skus s ON s.sku_id = c.sku_id
                WHERE c.cart_item_id = ? AND c.user_id = ?
                """,
                (cart_item_id, user_id),
            ).fetchone()
            if stock is not None and quantity > int(stock["stock_qty"]):
                raise CartStockError(t("cart.error.sold_out", stock=int(stock["stock_qty"])))
            cur = conn.execute(
                "UPDATE cart_items SET quantity = ? WHERE cart_item_id = ? AND user_id = ?",
                (quantity, cart_item_id, user_id),
            )
            if cur.rowcount == 0:
                raise CartNotFoundError(t("cart.error.no_line", cart_item_id=cart_item_id))
            conn.commit()
            row = conn.execute(
                """
                SELECT c.cart_item_id, c.product_id, c.sku_id, c.selected_options_json,
                       c.quantity, c.unit_price, c.selected, p.title
                FROM cart_items c JOIN products p ON p.product_id = c.product_id
                WHERE c.cart_item_id = ?
                """,
                (cart_item_id,),
            ).fetchone()
        return self._line_from_row(row)

    def remove_item(self, cart_item_id: int, user_id: str = DEMO_USER_ID) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM cart_items WHERE cart_item_id = ? AND user_id = ?",
                (cart_item_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def clear(self, user_id: str = DEMO_USER_ID) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
            conn.commit()
            return cur.rowcount

    # ------------------------------ Checkout ------------------------------

    def build_order(
        self,
        user_id: str = DEMO_USER_ID,
        address: str = DEFAULT_ADDRESS,
        clear_after: bool = True,
        order_user_id: str | None = None,
        cart_item_ids: list[int] | None = None,
    ) -> OrderSummary:
        """用购物车行下单：写入订单并移出已下单的行，两步在同一事务里。

        user_id 是购物车的归属（目前全局只有 demo_user 一辆车）；order_user_id 是下单的人，
        历史订单和「再买一次」按它查。cart_item_ids 指定下单哪几行，不传则取勾选项。
        价格按当前 SKU 价格结算，不用加车时的价格。
        """
        lines = self.list_items(user_id)
        if cart_item_ids is not None:
            wanted = set(cart_item_ids)
            lines = [line for line in lines if line.cart_item_id in wanted]
        else:
            lines = [line for line in lines if line.selected]
        if not lines:
            raise CartNotFoundError(t("cart.error.nothing_to_order"))
        with self._connect() as conn:
            lines = [self._reprice(conn, line) for line in lines]
            total = round(sum(line.subtotal for line in lines), 2)
            order_id, created_at = insert_order(
                conn,
                user_id=order_user_id or user_id,
                address=address,
                total=total,
                lines=lines,
            )
            if clear_after:
                conn.executemany(
                    "DELETE FROM cart_items WHERE cart_item_id = ? AND user_id = ?",
                    [(line.cart_item_id, user_id) for line in lines],
                )
            conn.commit()
        return OrderSummary(
            order_id=order_id,
            address=address,
            lines=lines,
            total=total,
            created_at=created_at,
        )

    def _reprice(self, conn: sqlite3.Connection, line: CartLine) -> CartLine:
        row = conn.execute(
            "SELECT price, stock_qty FROM product_skus WHERE sku_id = ? AND status = 'active'",
            (line.sku_id,),
        ).fetchone()
        if row is None:
            raise CartNotFoundError(t("cart.error.delisted", title=line.title))
        if int(row["stock_qty"]) < line.quantity:
            raise CartStockError(t("cart.error.short_stock", title=line.title, stock=int(row["stock_qty"])))
        return replace(line, unit_price=float(row["price"]))

    # ------------------------------ Internal ------------------------------

    @staticmethod
    def _require_stock(conn: sqlite3.Connection, user_id: str, sku: sqlite3.Row, quantity: int) -> None:
        """Reject an add that the SKU cannot supply, counting what is already in the cart."""
        row = conn.execute(
            "SELECT quantity FROM cart_items WHERE user_id = ? AND sku_id = ?", (user_id, sku["sku_id"])
        ).fetchone()
        in_cart = int(row["quantity"]) if row is not None else 0
        if in_cart + quantity > int(sku["stock_qty"]):
            raise CartStockError(t("cart.error.sold_out", stock=int(sku["stock_qty"])))

    def _resolve_sku(
        self, conn: sqlite3.Connection, product_id: str, sku_id: str | None
    ) -> sqlite3.Row | None:
        if sku_id:
            return conn.execute(
                "SELECT sku_id, properties_json, price, stock_qty FROM product_skus "
                "WHERE sku_id = ? AND product_id = ? AND status = 'active'",
                (sku_id, product_id),
            ).fetchone()
        # Default to the cheapest active SKU.
        return conn.execute(
            "SELECT sku_id, properties_json, price, stock_qty FROM product_skus "
            "WHERE product_id = ? AND status = 'active' ORDER BY stock_qty > 0 DESC, price ASC, sku_id ASC LIMIT 1",
            (product_id,),
        ).fetchone()

    def _line_from_row(self, row: sqlite3.Row) -> CartLine:
        return CartLine(
            cart_item_id=int(row["cart_item_id"]),
            product_id=row["product_id"],
            sku_id=row["sku_id"],
            title=row["title"],
            options=load_json_text(row["selected_options_json"]),
            quantity=int(row["quantity"]),
            unit_price=float(row["unit_price"]),
            selected=bool(row["selected"]),
        )

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"SQLite 商品库不存在：{self.db_path}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
