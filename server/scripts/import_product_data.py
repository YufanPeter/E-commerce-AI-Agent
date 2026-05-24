#!/usr/bin/env python3
"""Import product JSON files into the MVP SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = ROOT_DIR / "server" / "db" / "ecommerce_agent.sqlite3"
DEFAULT_INIT_SQL = ROOT_DIR / "server" / "db" / "init.sql"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import data/*/data/*.json product files into SQLite."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Product data root. Default: {DEFAULT_DATA_DIR}",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path. Default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--init-sql",
        type=Path,
        default=DEFAULT_INIT_SQL,
        help=f"Schema SQL path. Default: {DEFAULT_INIT_SQL}",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def short_text(text: str, limit: int = 72) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."


def derive_tags(product: dict[str, Any]) -> list[str]:
    tags = [
        product.get("category"),
        product.get("sub_category"),
        product.get("brand"),
    ]
    return [tag for tag in tags if isinstance(tag, str) and tag.strip()]


def sku_price_range(product: dict[str, Any]) -> tuple[float, float]:
    prices = [
        float(sku["price"])
        for sku in product.get("skus", [])
        if "price" in sku
    ]
    if not prices:
        base_price = float(product["base_price"])
        return base_price, base_price
    return min(prices), max(prices)


def init_database(conn: sqlite3.Connection, init_sql: Path) -> None:
    with init_sql.open("r", encoding="utf-8") as file:
        conn.executescript(file.read())


def import_product(
    conn: sqlite3.Connection,
    product: dict[str, Any],
    source_path: Path,
    data_dir: Path,
) -> None:
    product_id = product["product_id"]
    rag_knowledge = product.get("rag_knowledge", {})
    marketing_description = rag_knowledge.get("marketing_description", "")
    min_price, max_price = sku_price_range(product)
    relative_source = source_path.relative_to(data_dir.parent)

    conn.execute(
        """
        INSERT INTO products (
            product_id,
            title,
            brand,
            category,
            sub_category,
            summary,
            recommend_reason,
            tags_json,
            base_price,
            min_price,
            max_price,
            image_path,
            image_url,
            source_path,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        ON CONFLICT(product_id) DO UPDATE SET
            title = excluded.title,
            brand = excluded.brand,
            category = excluded.category,
            sub_category = excluded.sub_category,
            summary = excluded.summary,
            recommend_reason = excluded.recommend_reason,
            tags_json = excluded.tags_json,
            base_price = excluded.base_price,
            min_price = excluded.min_price,
            max_price = excluded.max_price,
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
            short_text(marketing_description),
            short_text(marketing_description),
            json_text(derive_tags(product)),
            float(product["base_price"]),
            min_price,
            max_price,
            product.get("image_path"),
            None,
            str(relative_source),
        ),
    )

    conn.execute(
        """
        INSERT INTO product_contents (
            product_id,
            marketing_description,
            official_faq_json,
            user_reviews_json
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            marketing_description = excluded.marketing_description,
            official_faq_json = excluded.official_faq_json,
            user_reviews_json = excluded.user_reviews_json
        """,
        (
            product_id,
            marketing_description,
            json_text(rag_knowledge.get("official_faq", [])),
            json_text(rag_knowledge.get("user_reviews", [])),
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


def import_all(data_dir: Path, db_path: Path, init_sql: Path) -> tuple[int, int]:
    data_dir = data_dir.resolve()
    db_path = db_path.resolve()
    init_sql = init_sql.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    product_paths = sorted(data_dir.glob("*/data/*.json"))
    if not product_paths:
        raise FileNotFoundError(f"No product JSON files found under {data_dir}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        init_database(conn, init_sql)
        with conn:
            for path in product_paths:
                import_product(conn, load_json(path), path.resolve(), data_dir)

        sku_count = conn.execute("SELECT COUNT(*) FROM product_skus").fetchone()[0]
        return len(product_paths), int(sku_count)
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    product_count, sku_count = import_all(args.data_dir, args.db, args.init_sql)
    print(f"Imported {product_count} products and {sku_count} SKUs into {args.db}")


if __name__ == "__main__":
    main()
