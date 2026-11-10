"""Build a new, isolated Amazon snapshot database from the supplied dataset."""

from __future__ import annotations

import argparse
from decimal import Decimal
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from catalog_config import AMAZON_CATEGORIES, AMAZON_DATA_DIR, AMAZON_DB_PATH
from store.import_product_data import DEFAULT_INIT_SQL, import_product, init_database, load_json


SCHEMA = DEFAULT_INIT_SQL.with_name("amazon_catalog.sql")
ASIN = re.compile(r"[A-Z0-9]{10}")


def build_amazon_catalog(data_dir: Path, db_path: Path) -> int:
    """Validate and publish a complete snapshot; an existing database is never replaced."""
    data_dir, db_path = data_dir.resolve(), db_path.resolve()
    if db_path.exists():
        raise FileExistsError(f"Database already exists; choose another output path: {db_path}")
    report = load_json(data_dir / "sample_report.json")
    manifest = load_json(data_dir / "image_manifest.json")
    product_paths = sorted(data_dir.glob("*/data/*.json"))
    if not product_paths or len(product_paths) != len(manifest) or len(product_paths) != report["summary"]["products"]:
        raise ValueError("Product files, image manifest, and sample report disagree")
    exchange_rate = Decimal(str(report["usd_cny"]))
    if not exchange_rate.is_finite() or exchange_rate <= 0:
        raise ValueError("Invalid source exchange rate")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".amazon-build-", dir=db_path.parent) as tmp:
        staging = Path(tmp) / "catalog.sqlite3"
        conn = sqlite3.connect(staging)
        try:
            init_database(conn, DEFAULT_INIT_SQL)
            init_database(conn, SCHEMA)
            seen: set[str] = set()
            with conn:
                for path in product_paths:
                    product = load_json(path)
                    source = product["source"]
                    asin, pid = source["parent_asin"], product["product_id"]
                    if not ASIN.fullmatch(asin) or pid != f"amz_{asin}" or path.stem != pid or pid in seen:
                        raise ValueError(f"Invalid or duplicate product ID: {path.name}")
                    if source["dataset"] != "amazon_reviews_2023" or not source["cell"].strip():
                        raise ValueError(f"Missing source or category path: {pid}")
                    image_url = manifest.get(pid)
                    if not isinstance(image_url, str) or image_url != source["image_url"]:
                        raise ValueError(f"Image manifest mismatch: {pid}")
                    url = urlsplit(image_url)
                    if url.scheme != "https" or not url.hostname or url.username or url.password:
                        raise ValueError(f"Invalid image URL: {pid}")
                    usd = Decimal(str(source["price_usd"]))
                    cny = Decimal(str(product["base_price"]))
                    if (not usd.is_finite() or not cny.is_finite() or usd <= 0 or cny <= 0
                            or abs(cny - usd * exchange_rate) > Decimal("0.005")):
                        raise ValueError(f"Reference prices or exchange rate disagree: {pid}")
                    # The export's default SKU is a placeholder, never a purchasable variant.
                    knowledge = product["rag_knowledge"]
                    snapshot = {
                        **product,
                        "category": AMAZON_CATEGORIES[product["category"]],
                        "sub_category": source["cell"].rsplit(" > ", 1)[-1],
                        "base_price": float(usd),
                        "skus": [],
                        "image_path": None,
                        "rag_knowledge": {
                            **knowledge,
                            "user_reviews": [
                                {**review, "nickname": "Amazon customer"}
                                for review in knowledge.get("user_reviews", [])
                            ],
                        },
                    }
                    import_product(conn, snapshot, path, data_dir, image_url=image_url)
                    conn.execute(
                        "INSERT INTO amazon_product_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (pid, asin, source["dataset"], source["cell"], source["amazon_category"],
                         float(usd), float(exchange_rate), source["average_rating"], source["rating_number"]),
                    )
                    seen.add(pid)
                if seen != set(manifest) or conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Amazon catalog contains orphaned records")
            # Close with a self-contained database file before publishing it.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            conn.close()
        os.link(staging, db_path)  # Atomic publish without clobbering an existing file.
    return len(product_paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=AMAZON_DATA_DIR)
    parser.add_argument("--db", type=Path, default=AMAZON_DB_PATH)
    args = parser.parse_args()
    print(f"Built Amazon catalog: {build_amazon_catalog(args.data_dir, args.db):,} products in {args.db}")


if __name__ == "__main__":
    main()
