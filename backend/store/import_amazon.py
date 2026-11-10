"""Build a new, isolated Amazon snapshot database from the supplied dataset."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from catalog_config import AMAZON_CATEGORIES, AMAZON_DATA_DIR, AMAZON_DB_PATH
from store.attribute_rules import ATTRIBUTE_RULES_VERSION, extract_attributes
from store.import_product_data import DEFAULT_INIT_SQL, import_product, init_database, load_json
from store.synthesize_commerce import load_rules, synthesize_skus


SCHEMA = DEFAULT_INIT_SQL.with_name("amazon_catalog.sql")
TAXONOMY_PATH = DEFAULT_INIT_SQL.with_name("amazon_taxonomy.json")
ASIN = re.compile(r"[A-Z0-9]{10}")
_HTML = re.compile(r"<[a-zA-Z/][^>]*>|&[a-z]+;|&#\d+;")
_GENERIC_BRANDS = {"generic", "unbranded", "unknown", "no brand"}
SHORT_DESCRIPTION_CHARS = 100
_VOLATILE_COLUMNS = {"created_at", "updated_at"}


def load_taxonomy_file(path: Path = TAXONOMY_PATH) -> dict[str, Any]:
    return load_json(path)


def brand_key(brand: str) -> str:
    """Spelling-insensitive key: ``L’Oréal Paris`` and ``L'Oréal Paris`` collapse together."""
    text = unicodedata.normalize("NFKC", brand).casefold()
    return re.sub(r"[\W_]+", "", text)


def canonical_brands(brands: list[str]) -> dict[str, str]:
    """Map each spelling to the most common variant, breaking ties alphabetically."""
    counts = Counter(brands)
    variants: dict[str, list[str]] = defaultdict(list)
    for brand in counts:
        variants[brand_key(brand)].append(brand)
    mapping: dict[str, str] = {}
    for spellings in variants.values():
        best = min(spellings, key=lambda spelling: (-counts[spelling], spelling))
        mapping.update({spelling: best for spelling in spellings})
    return mapping


def clean_description(text: str) -> str:
    """Remove markup only; listing wording is preserved."""
    if not _HTML.search(text):
        return text
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _digest(text: str) -> str:
    return hashlib.sha1(" ".join(text.casefold().split()).encode("utf-8")).hexdigest()[:16]


def duplicate_keys(products: list[dict[str, Any]]) -> dict[str, str]:
    """Return a shared key for products whose description or title is repeated."""
    descriptions = Counter(_digest(p["description"]) for p in products if p["description"])
    titles = Counter(_digest(p["title"]) for p in products)
    keys: dict[str, str] = {}
    for product in products:
        description = _digest(product["description"]) if product["description"] else None
        title = _digest(product["title"])
        if description and descriptions[description] > 1:
            keys[product["product_id"]] = f"d:{description}"
        elif titles[title] > 1:
            keys[product["product_id"]] = f"t:{title}"
    return keys


def _validated(path: Path, manifest: dict[str, str], exchange_rate: Decimal, seen: set[str]) -> dict[str, Any]:
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
    seen.add(pid)
    return product


def build_amazon_catalog(data_dir: Path, db_path: Path, taxonomy_path: Path = TAXONOMY_PATH) -> int:
    """Validate and publish a complete snapshot; an existing database is never replaced."""
    data_dir, db_path = data_dir.resolve(), db_path.resolve()
    if db_path.exists():
        raise FileExistsError(f"Database already exists; choose another output path: {db_path}")
    report = load_json(data_dir / "sample_report.json")
    manifest = load_json(data_dir / "image_manifest.json")
    taxonomy = load_json(taxonomy_path)
    nodes = {node["path"]: node for node in taxonomy["nodes"]}
    variant_rules = load_rules()
    product_paths = sorted(data_dir.glob("*/data/*.json"))
    if not product_paths or len(product_paths) != len(manifest) or len(product_paths) != report["summary"]["products"]:
        raise ValueError("Product files, image manifest, and sample report disagree")
    exchange_rate = Decimal(str(report["usd_cny"]))
    if not exchange_rate.is_finite() or exchange_rate <= 0:
        raise ValueError("Invalid source exchange rate")

    seen: set[str] = set()
    products = [_validated(path, manifest, exchange_rate, seen) for path in product_paths]
    if seen != set(manifest):
        raise ValueError("Amazon catalog contains orphaned records")
    unmapped = sorted({p["source"]["cell"] for p in products} - set(nodes))
    if unmapped:
        raise ValueError(f"Category paths missing from the taxonomy: {unmapped[:5]}")

    brands = canonical_brands([p["brand"] for p in products])
    duplicates = duplicate_keys([
        {"product_id": p["product_id"], "title": p["title"],
         "description": clean_description(p["rag_knowledge"]["marketing_description"])}
        for p in products
    ])

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".amazon-build-", dir=db_path.parent) as tmp:
        staging = Path(tmp) / "catalog.sqlite3"
        conn = sqlite3.connect(staging)
        try:
            init_database(conn, DEFAULT_INIT_SQL)
            init_database(conn, SCHEMA)
            with conn:
                for node in taxonomy["nodes"]:
                    category = _category_for(node["path"], products)
                    conn.execute(
                        "INSERT INTO catalog_taxonomy VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (node["label"], node["path"], category, node["group"], node["role"], node["audience"],
                         json.dumps(node["terms"], ensure_ascii=False),
                         json.dumps(node["accessory_for"], ensure_ascii=False)),
                    )
                for path, product in zip(product_paths, products):
                    _insert_product(conn, path, product, data_dir, nodes, brands, duplicates, manifest, exchange_rate, variant_rules)
                build_info = {
                    "schema_version": "amazon-catalog-v2",
                    "taxonomy_version": taxonomy["version"],
                    "attribute_rules_version": ATTRIBUTE_RULES_VERSION,
                    "dataset": "amazon_dense_10k",
                    "commerce_source": variant_rules["version"],
                    "product_count": str(len(products)),
                }
                conn.executemany("INSERT INTO catalog_build_info VALUES (?, ?)", build_info.items())
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Amazon catalog contains orphaned records")
            # Close with a self-contained database file before publishing it.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            conn.close()
        os.link(staging, db_path)  # Atomic publish without clobbering an existing file.
    return len(products)


def _category_for(path: str, products: list[dict[str, Any]]) -> str:
    for product in products:
        if product["source"]["cell"] == path:
            return AMAZON_CATEGORIES[product["category"]]
    return ""


def _insert_product(
    conn: sqlite3.Connection,
    path: Path,
    product: dict[str, Any],
    data_dir: Path,
    nodes: dict[str, dict[str, Any]],
    brands: dict[str, str],
    duplicates: dict[str, str],
    manifest: dict[str, str],
    exchange_rate: Decimal,
    variant_rules: dict[str, Any],
) -> None:
    source = product["source"]
    pid, node = product["product_id"], nodes[source["cell"]]
    usd = Decimal(str(source["price_usd"]))
    knowledge = product["rag_knowledge"]
    description = clean_description(knowledge["marketing_description"])
    brand = brands[product["brand"]]
    category = AMAZON_CATEGORIES[product["category"]]
    # The export's default SKU is a placeholder, never a purchasable variant.
    snapshot = {
        **product,
        "brand": brand,
        "category": category,
        "sub_category": node["label"],
        "base_price": float(usd),
        "skus": [],
        "image_path": None,
        "rag_knowledge": {
            **knowledge,
            "marketing_description": description,
            "user_reviews": [
                {**review, "nickname": "Amazon customer"} for review in knowledge.get("user_reviews", [])
            ],
        },
    }
    import_product(conn, snapshot, path, data_dir, image_url=manifest[pid])
    conn.execute(
        "INSERT INTO amazon_product_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, source["parent_asin"], source["dataset"], source["cell"], source["amazon_category"],
         float(usd), float(exchange_rate), source["average_rating"], source["rating_number"],
         product["brand"], int(brand.casefold() in _GENERIC_BRANDS), node["group"], node["role"], node["audience"],
         duplicates.get(pid), int(len(description) < SHORT_DESCRIPTION_CHARS)),
    )
    attributes = extract_attributes(category, product["title"], description)
    for row in attributes:
        conn.execute(
            "INSERT OR IGNORE INTO product_attributes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, row["attribute"], row["value"], row["status"], row["evidence_field"],
             row["evidence_start"], row["evidence_end"], row["provenance"]),
        )
    observed_colors = [row["value"].title() for row in attributes if row["attribute"] == "color"]
    for sku in synthesize_skus(source["parent_asin"], node["label"], float(usd), observed_colors, variant_rules):
        conn.execute(
            "INSERT INTO product_skus (sku_id, product_id, properties_json, price, stock_qty) VALUES (?, ?, ?, ?, ?)",
            (sku["sku_id"], pid, json.dumps(sku["properties"], ensure_ascii=False, separators=(",", ":")),
             sku["price"], sku["stock_qty"]),
        )


def content_digest(db_path: Path) -> str:
    """Hash catalog content, ignoring build timestamps and runtime tables."""
    tables = ("products", "product_skus", "product_descriptions", "product_reviews", "amazon_product_metadata",
              "catalog_taxonomy", "product_attributes", "catalog_build_info")
    digest = hashlib.sha256()
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        for table in tables:
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
                       if row[1] not in _VOLATILE_COLUMNS]
            selected = ", ".join(f'"{column}"' for column in columns)
            # Model-extracted attributes are added after the build by the graph step.
            scope = " WHERE provenance <> 'llm'" if table == "product_attributes" else ""
            for row in conn.execute(f'SELECT {selected} FROM "{table}"{scope} ORDER BY {selected}'):
                digest.update(repr(row).encode("utf-8"))
    finally:
        conn.close()
    return digest.hexdigest()


def coverage_report(db_path: Path) -> dict[str, Any]:
    """Share of products per category with at least one recorded value for each attribute."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        totals = dict(conn.execute("SELECT category, COUNT(*) FROM products GROUP BY category"))
        rows = conn.execute(
            """SELECT p.category, a.attribute, a.status, COUNT(DISTINCT a.product_id)
               FROM product_attributes a JOIN products p USING (product_id)
               GROUP BY p.category, a.attribute, a.status"""
        ).fetchall()
        taxonomy = conn.execute("SELECT COUNT(DISTINCT sub_category) FROM products").fetchone()[0]
        skus, sold_out = conn.execute("SELECT COUNT(*), SUM(stock_qty = 0) FROM product_skus").fetchone()
    finally:
        conn.close()
    report: dict[str, Any] = {
        "sub_categories": taxonomy, "skus": skus, "out_of_stock_share": round(sold_out / skus, 3), "attributes": {},
    }
    for category, attribute, status, count in rows:
        entry = report["attributes"].setdefault(category, {}).setdefault(attribute, {})
        entry[status] = round(count / totals[category], 3)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=AMAZON_DATA_DIR)
    parser.add_argument("--db", type=Path, default=AMAZON_DB_PATH)
    args = parser.parse_args()
    count = build_amazon_catalog(args.data_dir, args.db)
    db = args.db.resolve()
    report = {"products": count, "content_digest": content_digest(db), **coverage_report(db)}
    db.with_name("build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Built Amazon catalog: {count:,} products in {db}")


if __name__ == "__main__":
    main()
