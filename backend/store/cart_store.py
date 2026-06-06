from __future__ import annotations

"""购物车 SQLite 访问层。

承接 init.sql 里已建好的 cart_items 表，提供对话式购物车需要的 CRUD：
    - add_item / set_quantity / remove_item / clear
    - list_items（带商品标题、SKU 规格、单价、小计）
    - build_order（从勾选项算订单汇总，下单后 clear）

设计取舍：
    - 演示期单用户：默认 user_id = 'demo_user'（init.sql 已 seed）。
      多用户时把 user_id 换成会话/登录态即可，接口已留参数。
    - 不新建 orders 表：下单返回一份结构化订单汇总并清空购物车，
      达成"业务闭环"的演示目标，避免一次性引入订单状态机的复杂度。
    - 加车默认选最便宜的在售 SKU——对话里用户很少指定规格，
      "把这款加进来"应当能直接成交，需要换规格再说"要XX色的"。
"""

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from store.product_store import DEFAULT_DB_PATH, load_json_text


DEMO_USER_ID = "demo_user"
DEFAULT_ADDRESS = "默认地址（北京市朝阳区示例路 1 号 · 收货人：Demo User · 138****0000）"


@dataclass(frozen=True)
class CartLine:
    """购物车一行（一个 SKU）。"""

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
    """一次下单的结构化结果。"""

    order_id: str
    address: str
    lines: list[CartLine]
    total: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "address": self.address,
            "lines": [line.to_dict() for line in self.lines],
            "item_count": sum(line.quantity for line in self.lines),
            "total": self.total,
        }


class CartNotFoundError(Exception):
    """目标商品/购物车行不存在时抛出，由 tool 转成友好话术。"""


class CartStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    # ------------------------------ 读 ------------------------------

    def list_items(self, user_id: str = DEMO_USER_ID) -> list[CartLine]:
        """按加入顺序返回购物车明细（join 商品标题）。"""
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
        """列出某商品全部在售 SKU（规格 + 价格），按价格升序。

        给 CartTool 判断"是否需要问用户选规格"用：当返回多于一个时，
        意味着加购前应让用户在颜色/尺码/容量等维度上做选择。
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
        """取商品标题；用于加购询问/确认话术。找不到时回退 product_id。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT title FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()
        return row["title"] if row else product_id

    # ------------------------------ 写 ------------------------------

    def add_product(
        self,
        product_id: str,
        user_id: str = DEMO_USER_ID,
        sku_id: str | None = None,
        quantity: int = 1,
    ) -> CartLine:
        """把商品加入购物车。

        sku_id 为空时自动选最便宜的在售 SKU。已在车里则累加数量
        （依赖 UNIQUE(user_id, sku_id) + UPSERT）。
        """
        if quantity <= 0:
            quantity = 1
        with self._connect() as conn:
            sku = self._resolve_sku(conn, product_id, sku_id)
            if sku is None:
                raise CartNotFoundError(f"商品 {product_id} 没有可购买的 SKU")
            conn.execute(
                """
                INSERT INTO cart_items
                    (user_id, product_id, sku_id, selected_options_json,
                     quantity, unit_price, selected)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id, sku_id) DO UPDATE SET
                    quantity = quantity + excluded.quantity,
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
        """设置某行数量；quantity<=0 等价于删除（返回 None）。"""
        if quantity <= 0:
            self.remove_item(cart_item_id, user_id)
            return None
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE cart_items SET quantity = ? WHERE cart_item_id = ? AND user_id = ?",
                (quantity, cart_item_id, user_id),
            )
            if cur.rowcount == 0:
                raise CartNotFoundError(f"购物车行 {cart_item_id} 不存在")
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

    # ------------------------------ 下单 ------------------------------

    def build_order(
        self,
        user_id: str = DEMO_USER_ID,
        address: str = DEFAULT_ADDRESS,
        clear_after: bool = True,
    ) -> OrderSummary:
        """用勾选中的购物车行生成订单汇总；下单后默认清空已下单项。"""
        lines = [line for line in self.list_items(user_id) if line.selected]
        if not lines:
            raise CartNotFoundError("购物车里没有可下单的商品")
        total = round(sum(line.subtotal for line in lines), 2)
        order = OrderSummary(
            order_id="ord_" + uuid.uuid4().hex[:12],
            address=address,
            lines=lines,
            total=total,
        )
        if clear_after:
            with self._connect() as conn:
                conn.executemany(
                    "DELETE FROM cart_items WHERE cart_item_id = ? AND user_id = ?",
                    [(line.cart_item_id, user_id) for line in lines],
                )
                conn.commit()
        return order

    # ------------------------------ 内部 ------------------------------

    def _resolve_sku(
        self, conn: sqlite3.Connection, product_id: str, sku_id: str | None
    ) -> sqlite3.Row | None:
        if sku_id:
            return conn.execute(
                "SELECT sku_id, properties_json, price FROM product_skus "
                "WHERE sku_id = ? AND product_id = ? AND status = 'active'",
                (sku_id, product_id),
            ).fetchone()
        # 默认最便宜的在售 SKU
        return conn.execute(
            "SELECT sku_id, properties_json, price FROM product_skus "
            "WHERE product_id = ? AND status = 'active' ORDER BY price ASC, sku_id ASC LIMIT 1",
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
