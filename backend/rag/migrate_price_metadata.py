from __future__ import annotations

"""给已建好的 Chroma 索引原地补上 SKU 价格区间元数据（min_price / max_price / sku_count）。

不重新 embedding：只用 collection.update 改 metadata，读 SQLite 的
product_price_ranges 作为事实来源。重建索引（build_chroma）时会自动带上，
这个脚本只服务已有索引的迁移。

用法：
    cd backend && python -m rag.migrate_price_metadata            # 百炼主索引
    cd backend && python -m rag.migrate_price_metadata --all      # 连豆包备用索引一起
"""

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Any

from rag.chroma_store import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_PERSIST_DIR,
    FALLBACK_COLLECTION,
    FALLBACK_EMBEDDING_MODEL,
    create_collection,
)

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "backend" / "storage" / "ecommerce_agent.sqlite3"


def load_price_ranges(db_path: Path = DEFAULT_DB_PATH) -> dict[str, dict[str, float | int]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT product_id, min_price, max_price, sku_count FROM product_price_ranges"
        ).fetchall()
    finally:
        conn.close()
    return {
        pid: {"min_price": float(lo), "max_price": float(hi), "sku_count": int(n or 0)}
        for pid, lo, hi, n in rows
    }


def patch_collection(collection: Any, ranges: dict[str, dict[str, float | int]], batch: int = 200) -> int:
    """把每个 chunk 的 metadata 合并上价格区间；返回更新的 chunk 数。"""
    updated = 0
    offset = 0
    while True:
        page = collection.get(include=["metadatas"], limit=batch, offset=offset)
        ids = page.get("ids") or []
        if not ids:
            break
        new_ids: list[str] = []
        new_meta: list[dict[str, Any]] = []
        for chunk_id, meta in zip(ids, page.get("metadatas") or []):
            meta = dict(meta or {})
            rng = ranges.get(str(meta.get("product_id", "")))
            if rng is None:
                base = float(meta.get("base_price", 0) or 0)
                rng = {"min_price": base, "max_price": base, "sku_count": 0}
            if all(meta.get(k) == v for k, v in rng.items()):
                continue
            meta.update(rng)
            new_ids.append(chunk_id)
            new_meta.append(meta)
        if new_ids:
            collection.update(ids=new_ids, metadatas=new_meta)
            updated += len(new_ids)
        offset += len(ids)
    return updated


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="给 Chroma 索引补 SKU 价格区间元数据")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--persist-dir", type=Path, default=DEFAULT_PERSIST_DIR)
    parser.add_argument("--all", action="store_true", help="连豆包备用索引一起迁移")
    args = parser.parse_args()

    ranges = load_price_ranges(args.db)
    logger.info("读到 %d 个商品的价格区间", len(ranges))

    targets = [(DEFAULT_COLLECTION, DEFAULT_EMBEDDING_MODEL, "dashscope")]
    if args.all and FALLBACK_EMBEDDING_MODEL:
        targets.append((FALLBACK_COLLECTION, FALLBACK_EMBEDDING_MODEL, "ark"))
    for name, model, provider in targets:
        collection = create_collection(
            persist_dir=args.persist_dir, collection_name=name, embedding_model=model, provider=provider,
        )
        n = patch_collection(collection, ranges)
        logger.info("%s：更新 %d 个 chunk（共 %d）", name, n, collection.count())


if __name__ == "__main__":
    main()
