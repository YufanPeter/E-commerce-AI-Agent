#!/usr/bin/env python3
"""Import product JSON data into the SQLite catalog."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = ROOT_DIR / "backend" / "storage" / "ecommerce_agent.sqlite3"
DEFAULT_INIT_SQL = ROOT_DIR / "backend" / "db" / "init.sql"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import product files from data/*/data/*.json into SQLite"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Product-data root directory (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--init-sql",
        type=Path,
        default=DEFAULT_INIT_SQL,
        help=f"Schema SQL path (default: {DEFAULT_INIT_SQL})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the existing SQLite file before importing so the latest schema is rebuilt",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def init_database(conn: sqlite3.Connection, init_sql: Path) -> None:
    with init_sql.open("r", encoding="utf-8") as file:
        conn.executescript(file.read())


def review_polarity(rating: int | float) -> str:
    if rating >= 4:
        return "positive"
    if rating <= 2:
        return "negative"
    return "neutral"


def reset_database(db_path: Path) -> None:
    for path in [db_path, db_path.with_suffix(db_path.suffix + "-wal"), db_path.with_suffix(db_path.suffix + "-shm")]:
        if path.exists():
            path.unlink()


def import_product(
    conn: sqlite3.Connection,
    product: dict[str, Any],
    source_path: Path,
    data_dir: Path,
) -> None:
    product_id = product["product_id"]
    rag_knowledge = product.get("rag_knowledge", {})
    marketing_description = rag_knowledge.get("marketing_description", "")
    relative_source = source_path.relative_to(data_dir.parent)

    conn.execute(
        """
        INSERT INTO products (
            product_id,
            title,
            brand,
            category,
            sub_category,
            base_price,
            image_path,
            image_url,
            source_path,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        ON CONFLICT(product_id) DO UPDATE SET
            title = excluded.title,
            brand = excluded.brand,
            category = excluded.category,
            sub_category = excluded.sub_category,
            base_price = excluded.base_price,
            image_path = excluded.image_path,
            image_url = excluded.image_url,
            source_path = excluded.source_path,
            status = excluded.status
        """,
        (
            product_id,
            product["title"],
            product["brand"],
            product["category"],
            product.get("sub_category"),
            float(product["base_price"]),
            product.get("image_path"),
            None,
            str(relative_source),
        ),
    )

    conn.execute(
        """
        INSERT INTO product_descriptions (
            product_id,
            marketing_description
        )
        VALUES (?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            marketing_description = excluded.marketing_description
        """,
        (
            product_id,
            marketing_description,
        ),
    )

    conn.execute("DELETE FROM product_faqs WHERE product_id = ?", (product_id,))
    for index, faq in enumerate(rag_knowledge.get("official_faq", [])):
        conn.execute(
            """
            INSERT INTO product_faqs (
                product_id,
                source_index,
                question,
                answer
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                product_id,
                index,
                faq.get("question", ""),
                faq.get("answer", ""),
            ),
        )

    conn.execute("DELETE FROM product_reviews WHERE product_id = ?", (product_id,))
    for index, review in enumerate(rag_knowledge.get("user_reviews", [])):
        rating = int(review.get("rating", 3))
        conn.execute(
            """
            INSERT INTO product_reviews (
                product_id,
                source_index,
                nickname,
                rating,
                content,
                polarity
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                index,
                review.get("nickname", ""),
                rating,
                review.get("content", ""),
                review_polarity(rating),
            ),
        )

    for sku in product.get("skus", []):
        conn.execute(
            """
            INSERT INTO product_skus (
                sku_id,
                product_id,
                properties_json,
                price,
                stock_qty,
                status
            )
            VALUES (?, ?, ?, ?, 999, 'active')
            ON CONFLICT(sku_id) DO UPDATE SET
                product_id = excluded.product_id,
                properties_json = excluded.properties_json,
                price = excluded.price,
                stock_qty = excluded.stock_qty,
                status = excluded.status
            """,
            (
                sku["sku_id"],
                product_id,
                json_text(sku.get("properties", {})),
                float(sku["price"]),
            ),
        )


def import_all(
    data_dir: Path,
    db_path: Path,
    init_sql: Path,
    reset: bool = False,
) -> tuple[int, int, int, int]:
    data_dir = data_dir.resolve()
    db_path = db_path.resolve()
    init_sql = init_sql.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reset:
        reset_database(db_path)

    product_paths = sorted(data_dir.glob("*/data/*.json"))
    if not product_paths:
        raise FileNotFoundError(f"No product JSON files found under {data_dir}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        init_database(conn, init_sql)
        with conn:
            for path in product_paths:
                import_product(conn, load_json(path), path.resolve(), data_dir)

        sku_count = conn.execute("SELECT COUNT(*) FROM product_skus").fetchone()[0]
        faq_count = conn.execute("SELECT COUNT(*) FROM product_faqs").fetchone()[0]
        review_count = conn.execute("SELECT COUNT(*) FROM product_reviews").fetchone()[0]
        return len(product_paths), int(sku_count), int(faq_count), int(review_count)
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    product_count, sku_count, faq_count, review_count = import_all(
        args.data_dir,
        args.db,
        args.init_sql,
        reset=args.reset,
    )
    print(
        f"已导入 {product_count} 个商品、{sku_count} 个 SKU、"
        f"{faq_count} 条 FAQ、{review_count} 条评价到 {args.db}"
    )


if __name__ == "__main__":
    main()
