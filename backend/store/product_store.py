from __future__ import annotations

"""SQLite 商品事实访问层。

ProductStore 只负责确定性商品事实：商品、SKU、价格区间、详情页内容、
FAQ 和评价。它不做语义检索，也不生成推荐理由；这些交给 Chroma 和
后续 Hybrid Search / Agent 处理。
"""

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "backend" / "storage" / "ecommerce_agent.sqlite3"


@dataclass(frozen=True)
class PriceRange:
    min_price: float
    max_price: float
    sku_count: int


@dataclass(frozen=True)
class ProductSku:
    sku_id: str
    product_id: str
    properties: dict[str, Any]
    price: float
    stock_qty: int
    status: str


@dataclass(frozen=True)
class ProductCandidate:
    product_id: str
    title: str
    brand: str
    category: str
    sub_category: str | None
    base_price: float
    image_path: str | None
    image_url: str | None
    price_range: PriceRange
    matched_skus: list[ProductSku]


@dataclass(frozen=True)
class ProductFaq:
    source_index: int
    question: str
    answer: str


@dataclass(frozen=True)
class ProductReview:
    source_index: int
    nickname: str
    rating: int
    content: str
    polarity: str


@dataclass(frozen=True)
class ProductDetail:
    product_id: str
    title: str
    brand: str
    category: str
    sub_category: str | None
    base_price: float
    image_path: str | None
    image_url: str | None
    price_range: PriceRange
    skus: list[ProductSku]
    marketing_description: str
    faqs: list[ProductFaq]
    reviews: list[ProductReview]


class ProductStore:
    """SQLite catalog queries used as the factual source for agent tools."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def find_candidates(
        self,
        category: str | None = None,
        sub_category: str | None = None,
        max_price: float | None = None,
        brand_include: Sequence[str] | None = None,
        brand_exclude: Sequence[str] | None = None,
        product_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[ProductCandidate]:
        """Filter candidate products by hard constraints.

        Price constraints use SKU ranges and matching SKU existence rather than one
        ``products.base_price`` value.
        """
        if product_ids is not None and not product_ids:
            return []

        clauses = ["p.status = 'active'"]
        params: list[Any] = []

        if category:
            clauses.append("p.category = ?")
            params.append(category)
        if sub_category:
            clauses.append("p.sub_category = ?")
            params.append(sub_category)
        if max_price is not None:
            clauses.append("pr.min_price <= ?")
            params.append(max_price)
        if brand_include:
            clauses.append(f"p.brand IN ({placeholders(len(brand_include))})")
            params.extend(brand_include)
        if brand_exclude:
            clauses.append(f"p.brand NOT IN ({placeholders(len(brand_exclude))})")
            params.extend(brand_exclude)
        if product_ids is not None:
            clauses.append(f"p.product_id IN ({placeholders(len(product_ids))})")
            params.extend(product_ids)

        sql = f"""
            SELECT
                p.product_id,
                p.title,
                p.brand,
                p.category,
                p.sub_category,
                p.base_price,
                p.image_path,
                p.image_url,
                pr.min_price,
                pr.max_price,
                pr.sku_count
            FROM products p
            JOIN product_price_ranges pr ON pr.product_id = p.product_id
            WHERE {' AND '.join(clauses)}
            ORDER BY pr.min_price ASC, p.product_id ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            self._candidate_from_row(
                row,
                matched_skus=self.get_matched_skus(row["product_id"], max_price=max_price),
            )
            for row in rows
        ]

    def get_products_by_ids(self, product_ids: Sequence[str]) -> list[ProductCandidate]:
        """Return product facts by ID while preserving input order."""
        if not product_ids:
            return []

        candidates = self.find_candidates(product_ids=product_ids)
        by_id = {candidate.product_id: candidate for candidate in candidates}
        return [by_id[product_id] for product_id in product_ids if product_id in by_id]

    def search_by_keyword(
        self, keyword: str, limit: int = 8, brand_only: bool = False
    ) -> list[ProductCandidate]:
        """Run a case-insensitive keyword match over the full catalog.

        Conversational tools use this to locate named products outside the current result
        list. Callers decide whether zero, one, or several matches require clarification.

        With ``brand_only=True``, only the brand field is matched so SKU values cannot be
        mistaken for a switch to another product.
        """
        kw = (keyword or "").strip()
        if not kw:
            return []
        like = f"%{kw.lower()}%"
        if brand_only:
            where_kw = "LOWER(p.brand) LIKE ?"
            params: list[Any] = [like, limit]
        else:
            where_kw = "(LOWER(p.brand) LIKE ? OR LOWER(p.title) LIKE ?)"
            params = [like, like, limit]
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT p.product_id
                FROM products p
                JOIN product_price_ranges pr ON pr.product_id = p.product_id
                WHERE p.status = 'active' AND {where_kw}
                ORDER BY pr.min_price ASC, p.product_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        ids = [row["product_id"] for row in rows]
        return self.get_products_by_ids(ids)

    def match_brands_in_text(self, text: str, limit: int = 8) -> list[ProductCandidate]:
        """Match raw utterance substrings against every catalog brand.

        This avoids tokenization and stopword maintenance. Brand names are split on
        whitespace, and any token of at least two characters may match. SKU values do not
        equal brand tokens and therefore cannot trigger a product switch.
        """
        t = (text or "").lower()
        if not t:
            return []
        with self.connect() as conn:
            brand_rows = conn.execute(
                "SELECT DISTINCT brand FROM products "
                "WHERE status = 'active' AND brand IS NOT NULL AND brand <> ''"
            ).fetchall()
            matched: list[str] = []
            for row in brand_rows:
                brand = row["brand"]
                for tok in brand.lower().split():
                    if len(tok) >= 2 and tok in t:
                        matched.append(brand)
                        break
            if not matched:
                return []
            placeholders = ",".join("?" for _ in matched)
            id_rows = conn.execute(
                f"""
                SELECT p.product_id
                FROM products p
                JOIN product_price_ranges pr ON pr.product_id = p.product_id
                WHERE p.status = 'active' AND p.brand IN ({placeholders})
                ORDER BY pr.min_price ASC, p.product_id ASC
                LIMIT ?
                """,
                [*matched, limit],
            ).fetchall()
        ids = [row["product_id"] for row in id_rows]
        return self.get_products_by_ids(ids)

    def get_product_detail(self, product_id: str) -> ProductDetail | None:
        """Return the complete structured product data required by the detail page."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    p.product_id,
                    p.title,
                    p.brand,
                    p.category,
                    p.sub_category,
                    p.base_price,
                    p.image_path,
                    p.image_url,
                    pr.min_price,
                    pr.max_price,
                    pr.sku_count,
                    COALESCE(d.marketing_description, '') AS marketing_description
                FROM products p
                JOIN product_price_ranges pr ON pr.product_id = p.product_id
                LEFT JOIN product_descriptions d ON d.product_id = p.product_id
                WHERE p.product_id = ? AND p.status = 'active'
                """,
                (product_id,),
            ).fetchone()

        if row is None:
            return None

        return ProductDetail(
            product_id=row["product_id"],
            title=row["title"],
            brand=row["brand"],
            category=row["category"],
            sub_category=row["sub_category"],
            base_price=float(row["base_price"]),
            image_path=row["image_path"],
            image_url=row["image_url"],
            price_range=PriceRange(
                min_price=float(row["min_price"]),
                max_price=float(row["max_price"]),
                sku_count=int(row["sku_count"]),
            ),
            skus=self.get_skus(product_id),
            marketing_description=row["marketing_description"],
            faqs=self.get_faqs(product_id),
            reviews=self.get_reviews(product_id),
        )

    def get_skus(
        self,
        product_id: str,
        max_price: float | None = None,
    ) -> list[ProductSku]:
        """Return product SKUs, optionally limited by a maximum price."""
        clauses = ["product_id = ?", "status = 'active'"]
        params: list[Any] = [product_id]
        if max_price is not None:
            clauses.append("price <= ?")
            params.append(max_price)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT sku_id, product_id, properties_json, price, stock_qty, status
                FROM product_skus
                WHERE {' AND '.join(clauses)}
                ORDER BY price ASC, sku_id ASC
                """,
                params,
            ).fetchall()
        return [sku_from_row(row) for row in rows]

    def get_matched_skus(
        self,
        product_id: str,
        max_price: float | None = None,
        limit: int = 3,
    ) -> list[ProductSku]:
        """Return a representative in-budget SKU for price-filter explanations."""
        skus = self.get_skus(product_id, max_price=max_price)
        if max_price is None:
            return skus[:limit]
        return skus[:limit]

    def get_faqs(self, product_id: str) -> list[ProductFaq]:
        """Return official product FAQ entries."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT source_index, question, answer
                FROM product_faqs
                WHERE product_id = ?
                ORDER BY source_index ASC
                """,
                (product_id,),
            ).fetchall()
        return [
            ProductFaq(
                source_index=int(row["source_index"]),
                question=row["question"],
                answer=row["answer"],
            )
            for row in rows
        ]

    def get_reviews(
        self,
        product_id: str,
        polarity: str | None = None,
        limit: int | None = None,
    ) -> list[ProductReview]:
        """Return product reviews, optionally filtered by sentiment."""
        clauses = ["product_id = ?"]
        params: list[Any] = [product_id]
        if polarity:
            clauses.append("polarity = ?")
            params.append(polarity)

        sql = f"""
            SELECT source_index, nickname, rating, content, polarity
            FROM product_reviews
            WHERE {' AND '.join(clauses)}
            ORDER BY source_index ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            ProductReview(
                source_index=int(row["source_index"]),
                nickname=row["nickname"],
                rating=int(row["rating"]),
                content=row["content"],
                polarity=row["polarity"],
            )
            for row in rows
        ]

    def connect(self) -> sqlite3.Connection:
        """Create a SQLite connection configured with a row factory."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"SQLite 商品库不存在：{self.db_path}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _candidate_from_row(
        self,
        row: sqlite3.Row,
        matched_skus: list[ProductSku],
    ) -> ProductCandidate:
        return ProductCandidate(
            product_id=row["product_id"],
            title=row["title"],
            brand=row["brand"],
            category=row["category"],
            sub_category=row["sub_category"],
            base_price=float(row["base_price"]),
            image_path=row["image_path"],
            image_url=row["image_url"],
            price_range=PriceRange(
                min_price=float(row["min_price"]),
                max_price=float(row["max_price"]),
                sku_count=int(row["sku_count"]),
            ),
            matched_skus=matched_skus,
        )


def placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def load_json_text(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def sku_from_row(row: sqlite3.Row) -> ProductSku:
    return ProductSku(
        sku_id=row["sku_id"],
        product_id=row["product_id"],
        properties=load_json_text(row["properties_json"]),
        price=float(row["price"]),
        stock_qty=int(row["stock_qty"]),
        status=row["status"],
    )


def price_display(price_range: PriceRange) -> str:
    if price_range.min_price == price_range.max_price:
        return f"¥{price_range.min_price:g}"
    return f"¥{price_range.min_price:g} 起"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect SQLite product-store queries")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    subparsers = parser.add_subparsers(dest="command", required=True)

    candidates = subparsers.add_parser("candidates", help="Query candidates with hard filters")
    candidates.add_argument("--category")
    candidates.add_argument("--sub-category")
    candidates.add_argument("--max-price", type=float)
    candidates.add_argument("--brand-exclude", action="append", default=[])
    candidates.add_argument("--limit", type=int, default=5)

    detail = subparsers.add_parser("detail", help="Query product details")
    detail.add_argument("product_id")

    reviews = subparsers.add_parser("reviews", help="Query product reviews")
    reviews.add_argument("product_id")
    reviews.add_argument("--polarity", choices=["positive", "neutral", "negative"])
    reviews.add_argument("--limit", type=int, default=5)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = ProductStore(args.db)

    if args.command == "candidates":
        candidates = store.find_candidates(
            category=args.category,
            sub_category=args.sub_category,
            max_price=args.max_price,
            brand_exclude=args.brand_exclude,
            limit=args.limit,
        )
        for candidate in candidates:
            matched = "; ".join(
                f"{sku.sku_id} {sku.properties} ¥{sku.price:g}"
                for sku in candidate.matched_skus
            )
            print(
                f"{candidate.product_id} | {candidate.title} | "
                f"{price_display(candidate.price_range)} | matched_skus: {matched}"
            )

    if args.command == "detail":
        detail = store.get_product_detail(args.product_id)
        if detail is None:
            print("Product not found")
            return
        print(f"{detail.product_id} | {detail.title} | {price_display(detail.price_range)}")
        print(f"SKU: {len(detail.skus)} | FAQ: {len(detail.faqs)} | Reviews: {len(detail.reviews)}")
        print(detail.marketing_description[:160])

    if args.command == "reviews":
        for review in store.get_reviews(args.product_id, args.polarity, args.limit):
            print(f"{review.source_index} | {review.polarity} | {review.rating} | {review.content[:120]}")


if __name__ == "__main__":
    main()
