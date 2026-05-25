#!/usr/bin/env python3
"""Import product CDN image URLs from a manifest into SQLite."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "server" / "db" / "ecommerce_agent.sqlite3"
DEFAULT_IMAGE_MANIFEST = ROOT_DIR / "server" / "cdn" / "image_manifest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update products.image_url from a product_id -> image URL manifest."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path. Default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--image-manifest",
        type=Path,
        default=DEFAULT_IMAGE_MANIFEST,
        help=f"Product CDN URL manifest. Default: {DEFAULT_IMAGE_MANIFEST}",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if the manifest contains product IDs that do not exist in the database.",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Image manifest not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        manifest: Any = json.load(file)

    if not isinstance(manifest, dict):
        raise ValueError(f"Image manifest must be a JSON object: {path}")

    cleaned: dict[str, str] = {}
    for product_id, image_url in manifest.items():
        product_id = str(product_id).strip()
        image_url = str(image_url).strip()
        if not product_id or not image_url:
            raise ValueError("Image manifest contains an empty product_id or image_url")
        cleaned[product_id] = image_url
    return cleaned


def import_image_manifest(
    db_path: Path,
    image_manifest_path: Path,
    strict: bool,
) -> tuple[int, int, list[str]]:
    db_path = db_path.resolve()
    image_manifest_path = image_manifest_path.resolve()
    manifest = load_manifest(image_manifest_path)

    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")

        updated_count = 0
        missing_product_ids: list[str] = []
        with conn:
            for product_id, image_url in sorted(manifest.items()):
                cursor = conn.execute(
                    """
                    UPDATE products
                    SET image_url = ?
                    WHERE product_id = ?
                    """,
                    (image_url, product_id),
                )
                if cursor.rowcount == 0:
                    missing_product_ids.append(product_id)
                else:
                    updated_count += int(cursor.rowcount)

        if strict and missing_product_ids:
            missing = ", ".join(missing_product_ids[:20])
            suffix = "..." if len(missing_product_ids) > 20 else ""
            raise ValueError(f"Manifest product IDs not found in database: {missing}{suffix}")

        return len(manifest), updated_count, missing_product_ids
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    manifest_count, updated_count, missing_product_ids = import_image_manifest(
        args.db,
        args.image_manifest,
        args.strict,
    )
    print(
        f"Imported {updated_count}/{manifest_count} image URLs into {args.db}"
    )
    if missing_product_ids:
        print(f"Skipped {len(missing_product_ids)} missing products")


if __name__ == "__main__":
    main()
