"""Build the Amazon product and review vector indexes from the SQLite catalog.

Runs are resumable: IDs already in a collection are skipped, so an interrupted build can
be started again without paying for finished batches. Use ``--reset`` after the catalog
content changes, because resume only compares IDs.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Any

from catalog_config import (
    AMAZON_DB_PATH,
    AMAZON_INDEX_DIR,
    AMAZON_PRODUCT_COLLECTION,
    AMAZON_REVIEW_COLLECTION,
)
from rag.vector_index import ChromaVectorIndex


BATCH_SIZE = 20
_ATTEMPTS = 6
_RETRY_BASE_SECONDS = 2.0
_REVIEW_TITLE_CHARS = 80


@dataclass(frozen=True)
class Entry:
    id: str
    document: str
    metadata: dict[str, Any]


_PRODUCT_SQL = """
    SELECT p.product_id, p.title, p.brand, p.category, p.sub_category,
           a.category_path, a.catalog_group, a.product_role, a.is_generic_brand,
           d.marketing_description AS description, r.min_price, r.max_price
    FROM products p
    JOIN amazon_product_metadata a USING (product_id)
    JOIN product_descriptions d USING (product_id)
    JOIN product_price_ranges r USING (product_id)
    WHERE p.status = 'active'
    ORDER BY p.product_id
"""


def _filter_fields(row: sqlite3.Row) -> dict[str, Any]:
    """Metadata both indexes need for prefiltering. Chroma rejects ``None`` values."""
    return {
        "product_id": row["product_id"],
        "category": row["category"],
        "sub_category": row["sub_category"],
        "catalog_group": row["catalog_group"] or "",
        "product_role": row["product_role"],
        "brand": row["brand"],
        "min_price": float(row["min_price"]),
        "max_price": float(row["max_price"]),
    }


def load_entries(db_path: Path, limit: int | None = None) -> tuple[list[Entry], list[Entry]]:
    """Return (product entries, review entries); ``limit`` samples products for a trial."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_PRODUCT_SQL).fetchall()
        rows = rows[:limit] if limit else rows
        by_id = {row["product_id"]: row for row in rows}
        products = []
        for row in rows:
            lines = [row["title"]]
            if not row["is_generic_brand"]:
                lines.append(f"Brand: {row['brand']}")
            lines += [f"Category: {row['category_path']}", row["description"]]
            products.append(Entry(
                f"{row['product_id']}:product",
                "\n".join(line for line in lines if line),
                {**_filter_fields(row), "chunk_type": "marketing_description"},
            ))
        reviews = []
        for review in conn.execute(
            "SELECT product_id, source_index, rating, polarity, content FROM product_reviews "
            "WHERE content <> '' ORDER BY product_id, source_index"
        ):
            row = by_id.get(review["product_id"])
            if row is None:
                continue
            reviews.append(Entry(
                f"{review['product_id']}:review:{review['source_index']}",
                f"{row['title'][:_REVIEW_TITLE_CHARS]}\nReview: {review['content']}",
                {**_filter_fields(row), "chunk_type": "user_review",
                 "rating": int(review["rating"]), "polarity": review["polarity"]},
            ))
    finally:
        conn.close()
    return products, reviews


def _embed_with_retry(index: ChromaVectorIndex, documents: list[str]) -> list[list[float]]:
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            return index.embed(documents)
        except Exception as exc:  # noqa: BLE001 - transient API error types vary by provider
            if attempt == _ATTEMPTS:
                raise
            delay = _RETRY_BASE_SECONDS * 2 ** (attempt - 1)
            print(f"Embedding failed (attempt {attempt}/{_ATTEMPTS}): {type(exc).__name__}; retrying in {delay:.0f}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def build_collection(index: ChromaVectorIndex, entries: list[Entry], label: str, workers: int) -> int:
    """Embed missing entries concurrently and write them from one thread."""
    existing = index.existing_ids()
    pending = [entry for entry in entries if entry.id not in existing]
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    print(f"{label}: {len(entries)} entries, {len(existing)} already indexed, {len(batches)} batches to embed", flush=True)
    started = time.perf_counter()
    written = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Future[list[list[float]]], list[Entry]] = {
            pool.submit(_embed_with_retry, index, [entry.document for entry in batch]): batch
            for batch in batches
        }
        try:
            for future in as_completed(futures):
                batch = futures[future]
                index.upsert(
                    [entry.id for entry in batch],
                    [entry.document for entry in batch],
                    [entry.metadata for entry in batch],
                    future.result(),
                )
                written += len(batch)
                if written % 1000 < BATCH_SIZE or written == len(pending):
                    minutes = (time.perf_counter() - started) / 60
                    print(f"{label}: {written}/{len(pending)} written, {minutes:.1f} min", flush=True)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=AMAZON_DB_PATH)
    parser.add_argument("--index-dir", type=Path, default=AMAZON_INDEX_DIR)
    parser.add_argument("--limit", type=int, help="Index only the first N products, for a trial run.")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--reset", action="store_true", help="Delete both collections and rebuild them.")
    args = parser.parse_args()

    if args.reset:
        from rag.chroma_store import create_collection

        for name in (AMAZON_PRODUCT_COLLECTION, AMAZON_REVIEW_COLLECTION):
            create_collection(persist_dir=args.index_dir, collection_name=name, reset=True)
    products, reviews = load_entries(args.db, args.limit)
    for name, entries, label in (
        (AMAZON_PRODUCT_COLLECTION, products, "products"),
        (AMAZON_REVIEW_COLLECTION, reviews, "reviews"),
    ):
        index = ChromaVectorIndex(args.index_dir, name)
        build_collection(index, entries, label, args.workers)
        print(f"{label}: collection now holds {index.count()} vectors", flush=True)


if __name__ == "__main__":
    main()
