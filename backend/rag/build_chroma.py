from __future__ import annotations

"""Offline Chroma index builder for product RAG knowledge.

Run after the product JSON dataset changes. The builder creates semantic evidence chunks
from product facts, marketing descriptions, official FAQ entries, and user reviews.
"""

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from rag.chroma_store import (
    DEFAULT_COLLECTION,
    DEFAULT_DATA_DIR,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_PERSIST_DIR,
    create_collection,
)


@dataclass(frozen=True)
class Chunk:
    """Text unit to embed and write to Chroma."""

    id: str
    document: str
    metadata: dict[str, str | int | float | bool]


def iter_product_files(data_dir: Path) -> Iterable[Path]:
    """Yield product JSON files in stable order for reproducible builds."""
    yield from sorted(data_dir.glob("*/data/*.json"))


def load_product(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def compact_text(value: str) -> str:
    """Collapse redundant whitespace before embedding."""
    return " ".join(value.split())


def sku_summary(product: dict[str, Any]) -> str:
    """Build a compact SKU summary for the product-profile chunk."""
    summaries: list[str] = []
    for sku in product.get("skus", []):
        properties = sku.get("properties", {})
        property_text = "，".join(f"{key}：{value}" for key, value in properties.items())
        price = sku.get("price")
        summaries.append(f"{property_text}，价格：{price}元")
    return "；".join(summaries)


def base_metadata(
    product: dict[str, Any],
    chunk_type: str,
    source_index: int = 0,
) -> dict[str, str | int | float | bool]:
    """Build metadata shared by every chunk for one product."""
    return {
        "product_id": product["product_id"],
        "title": product["title"],
        "brand": product.get("brand", ""),
        "category": product.get("category", ""),
        "sub_category": product.get("sub_category", ""),
        "base_price": float(product.get("base_price", 0)),
        "image_path": product.get("image_path", ""),
        "chunk_type": chunk_type,
        "source_index": source_index,
    }


def product_prefix(product: dict[str, Any]) -> str:
    """Prefix evidence with product identity for clearer retrieval context."""
    return (
        f"商品：{product['title']}\n"
        f"品牌：{product.get('brand', '')}\n"
        f"类目：{product.get('category', '')} / {product.get('sub_category', '')}\n"
        f"基础价格：{product.get('base_price', '')}元\n"
    )


def build_product_profile_chunk(product: dict[str, Any]) -> Chunk:
    """Build a profile chunk containing title, category, price, and SKUs."""
    product_id = product["product_id"]
    sku_text = sku_summary(product)
    document = (
        f"{product_prefix(product)}"
        f"知识类型：商品基础信息\n"
        f"SKU规格：{sku_text}"
    )
    metadata = base_metadata(product, "product_profile")
    metadata["sku_summary"] = sku_text[:1000]
    return Chunk(
        id=f"{product_id}:profile",
        document=compact_text(document),
        metadata=metadata,
    )


def build_marketing_chunk(product: dict[str, Any]) -> Chunk | None:
    """Build a marketing chunk for use cases, selling points, and guidance."""
    text = product.get("rag_knowledge", {}).get("marketing_description", "")
    if not text:
        return None

    product_id = product["product_id"]
    document = (
        f"{product_prefix(product)}"
        f"知识类型：营销描述、卖点、适用场景、使用建议\n"
        f"内容：{text}"
    )
    return Chunk(
        id=f"{product_id}:marketing",
        document=compact_text(document),
        metadata=base_metadata(product, "marketing_description"),
    )


def build_faq_chunks(product: dict[str, Any]) -> list[Chunk]:
    """Build one chunk per FAQ so each question stays with its answer."""
    chunks: list[Chunk] = []
    faqs = product.get("rag_knowledge", {}).get("official_faq", [])
    for index, faq in enumerate(faqs):
        question = faq.get("question", "")
        answer = faq.get("answer", "")
        if not question and not answer:
            continue

        metadata = base_metadata(product, "official_faq", index)
        document = (
            f"{product_prefix(product)}"
            f"知识类型：官方FAQ、规格说明、注意事项\n"
            f"问题：{question}\n"
            f"回答：{answer}"
        )
        chunks.append(
            Chunk(
                id=f"{product['product_id']}:faq:{index}",
                document=compact_text(document),
                metadata=metadata,
            )
        )
    return chunks


def review_polarity(rating: int | float) -> str:
    """Map a numeric rating to coarse review sentiment."""
    if rating >= 4:
        return "positive"
    if rating <= 2:
        return "negative"
    return "neutral"


def build_review_chunks(product: dict[str, Any]) -> list[Chunk]:
    """Build one chunk per review for experiences and risk signals."""
    chunks: list[Chunk] = []
    reviews = product.get("rag_knowledge", {}).get("user_reviews", [])
    for index, review in enumerate(reviews):
        content = review.get("content", "")
        if not content:
            continue

        rating = review.get("rating", 0)
        metadata = base_metadata(product, "user_review", index)
        metadata["rating"] = int(rating)
        metadata["polarity"] = review_polarity(int(rating))
        metadata["nickname"] = review.get("nickname", "")

        document = (
            f"{product_prefix(product)}"
            f"知识类型：用户评价、真实体验、优点缺点、风险提示\n"
            f"用户：{review.get('nickname', '')}\n"
            f"评分：{rating}\n"
            f"内容：{content}"
        )
        chunks.append(
            Chunk(
                id=f"{product['product_id']}:review:{index}",
                document=compact_text(document),
                metadata=metadata,
            )
        )
    return chunks


def build_chunks(product: dict[str, Any]) -> list[Chunk]:
    """Build every Chroma chunk for one product object.

    Semantic fields define boundaries rather than a fixed character count: profile,
    marketing copy, each FAQ, and each review become separate evidence units.
    """
    chunks = [build_product_profile_chunk(product)]
    marketing_chunk = build_marketing_chunk(product)
    if marketing_chunk:
        chunks.append(marketing_chunk)
    chunks.extend(build_faq_chunks(product))
    chunks.extend(build_review_chunks(product))
    return chunks


def load_all_chunks(data_dir: Path) -> list[Chunk]:
    """Load all product files and convert them into Chroma chunks."""
    chunks: list[Chunk] = []
    product_files = list(iter_product_files(data_dir))
    if not product_files:
        raise FileNotFoundError(f"No product JSON files found under {data_dir}")

    for product_file in product_files:
        product = load_product(product_file)
        chunks.extend(build_chunks(product))
    return chunks


def batched(items: list[Chunk], size: int) -> Iterable[list[Chunk]]:
    """Yield fixed-size batches to bound embedding and upsert load."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def upsert_chunks(collection: Any, chunks: list[Chunk], batch_size: int) -> None:
    """Embed and upsert chunks in batches."""
    for chunk_batch in batched(chunks, batch_size):
        collection.upsert(
            ids=[chunk.id for chunk in chunk_batch],
            documents=[chunk.document for chunk in chunk_batch],
            metadatas=[chunk.metadata for chunk in chunk_batch],
        )


def print_stats(chunks: list[Chunk]) -> None:
    """Print index composition for a quick post-build check."""
    chunk_counts = Counter(chunk.metadata["chunk_type"] for chunk in chunks)
    category_counts = Counter(chunk.metadata["category"] for chunk in chunks)

    print(f"Loaded chunks: {len(chunks)}")
    print("Chunk types:")
    for chunk_type, count in sorted(chunk_counts.items()):
        print(f"  - {chunk_type}: {count}")
    print("Categories:")
    for category, count in sorted(category_counts.items()):
        print(f"  - {category}: {count}")


def parse_args() -> argparse.Namespace:
    """Parse offline index-build arguments."""
    parser = argparse.ArgumentParser(description="Build the Chroma RAG index.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--persist-dir", type=Path, default=DEFAULT_PERSIST_DIR)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Build the Chroma index from product JSON files."""
    args = parse_args()
    chunks = load_all_chunks(args.data_dir)
    print_stats(chunks)

    collection = create_collection(
        persist_dir=args.persist_dir,
        collection_name=args.collection,
        embedding_model=args.embedding_model,
        reset=args.reset,
    )
    upsert_chunks(collection, chunks, args.batch_size)
    print(f"Chroma collection ready: {args.collection}")
    print(f"Indexed chunks: {collection.count()}")
    print(f"Persist dir: {args.persist_dir}")


if __name__ == "__main__":
    main()
