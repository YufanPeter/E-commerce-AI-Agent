from __future__ import annotations

"""订单持久化：下单时把商品、规格、成交价做快照。

快照独立于 products 表：商品改价、下架、重建商品库都不影响历史订单，
所以这里不设外键。商品库用 --reset 重建时整个 SQLite 文件会被删除，订单也会一起没有；
上线前要把订单放到独立的库里。
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from store.product_store import DEFAULT_DB_PATH


_CREATE_ORDERS_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'placed',
    address    TEXT NOT NULL,
    total      REAL NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_LINES_SQL = """
CREATE TABLE IF NOT EXISTS order_lines (
    order_id     TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    product_id   TEXT NOT NULL,
    sku_id       TEXT NOT NULL,
    title        TEXT NOT NULL,
    brand        TEXT,
    category     TEXT,
    sub_category TEXT,
    options_json TEXT NOT NULL DEFAULT '{}',
    quantity     INTEGER NOT NULL,
    unit_price   REAL NOT NULL,
    PRIMARY KEY (order_id, line_no)
)
"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_orders_user_time ON orders (user_id, created_at)"


def ensure_order_tables(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_ORDERS_SQL)
    conn.execute(_CREATE_LINES_SQL)
    conn.execute(_CREATE_INDEX_SQL)


def insert_order(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    address: str,
    total: float,
    lines: list[Any],
) -> tuple[str, str]:
    """在调用方的事务里写订单，和清空购物车一起提交。返回 (订单号, 下单时间)。"""
    ensure_order_tables(conn)
    order_id = "ord_" + uuid.uuid4().hex[:12]
    # 微秒精度：同一秒连续下单也能排出先后
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    conn.execute(
        "INSERT INTO orders (order_id, user_id, address, total, created_at) VALUES (?, ?, ?, ?, ?)",
        (order_id, user_id, address, total, created_at),
    )
    for line_no, line in enumerate(lines):
        product = conn.execute(
            "SELECT brand, category, sub_category FROM products WHERE product_id = ?",
            (line.product_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO order_lines (
                order_id, line_no, product_id, sku_id, title, brand, category,
                sub_category, options_json, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, line_no, line.product_id, line.sku_id, line.title,
                product["brand"] if product else None,
                product["category"] if product else None,
                product["sub_category"] if product else None,
                json.dumps(line.options, ensure_ascii=False),
                line.quantity, line.unit_price,
            ),
        )
    return order_id, created_at


class OrderStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        ensure_order_tables(conn)
        return conn

    def list_orders(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """最近的订单在前，每单带完整商品行。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            orders: list[dict[str, Any]] = []
            for row in rows:
                lines = conn.execute(
                    "SELECT * FROM order_lines WHERE order_id = ? ORDER BY line_no",
                    (row["order_id"],),
                ).fetchall()
                orders.append({**dict(row), "lines": [_line_dict(line) for line in lines]})
        return orders

    def recent_products(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """买过的规格，同一规格只留最近一次。「再买一次」在这里面找。"""
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for order in self.list_orders(user_id, limit=limit):
            for line in order["lines"]:
                if line["sku_id"] in seen:
                    continue
                seen.add(line["sku_id"])
                out.append({**line, "ordered_at": order["created_at"], "order_id": order["order_id"]})
        return out


def _line_dict(row: sqlite3.Row) -> dict[str, Any]:
    line = dict(row)
    line["options"] = json.loads(line.pop("options_json") or "{}")
    line["subtotal"] = round(line["unit_price"] * line["quantity"], 2)
    return line
