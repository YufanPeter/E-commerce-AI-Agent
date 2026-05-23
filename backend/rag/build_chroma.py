from __future__ import annotations

"""商品 RAG 知识的离线 Chroma 索引构建脚本。

当商品 JSON 数据集变化时运行本脚本。脚本会从商品基础信息、营销描述、
官方 FAQ 和用户评价中构建语义证据 chunks，并写入本地 Chroma collection。
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
    """一个需要 embedding 并写入 Chroma 的文本单元。"""

    id: str
    document: str
    metadata: dict[str, str | int | float | bool]


def iter_product_files(data_dir: Path) -> Iterable[Path]:
    """按稳定顺序遍历商品 JSON，便于重复构建时结果可复现。"""
    yield from sorted(data_dir.glob("*/data/*.json"))


def load_product(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def compact_text(value: str) -> str:
    """在 embedding 前压缩多余空白字符。"""
    return " ".join(value.split())


def sku_summary(product: dict[str, Any]) -> str:
    """生成紧凑的 SKU 摘要，供商品基础信息 chunk 使用。"""
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
    """构建同一商品下所有 chunks 共享的 metadata。"""
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
    """给证据文本加上商品身份前缀，提升召回时的上下文清晰度。"""
    return (
        f"商品：{product['title']}\n"
        f"品牌：{product.get('brand', '')}\n"
        f"类目：{product.get('category', '')} / {product.get('sub_category', '')}\n"
        f"基础价格：{product.get('base_price', '')}元\n"
    )


def build_product_profile_chunk(product: dict[str, Any]) -> Chunk:
    """构建包含标题、类目、价格和 SKU 的商品基础信息 chunk。"""
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
    """构建用于召回场景、卖点和使用建议的营销描述 chunk。"""
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
    """每条 FAQ 构建一个 chunk，确保问题和答案不被拆开。"""
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
    """把数字评分映射成粗粒度评价倾向。"""
    if rating >= 4:
        return "positive"
    if rating <= 2:
        return "negative"
    return "neutral"


def build_review_chunks(product: dict[str, Any]) -> list[Chunk]:
    """每条用户评价构建一个 chunk，用于召回真实体验和风险提示。"""
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
    """为单个商品 JSON 构建所有 Chroma chunks。

    当前采用按字段语义切分，而不是固定字数切分：商品基础信息、营销描述、
    每条 FAQ、每条用户评价分别作为独立证据单元。
    """
    chunks = [build_product_profile_chunk(product)]
    marketing_chunk = build_marketing_chunk(product)
    if marketing_chunk:
        chunks.append(marketing_chunk)
    chunks.extend(build_faq_chunks(product))
    chunks.extend(build_review_chunks(product))
    return chunks


def load_all_chunks(data_dir: Path) -> list[Chunk]:
    """读取所有商品文件并转换为 Chroma chunks。"""
    chunks: list[Chunk] = []
    product_files = list(iter_product_files(data_dir))
    if not product_files:
        raise FileNotFoundError(f"No product JSON files found under {data_dir}")

    for product_file in product_files:
        product = load_product(product_file)
        chunks.extend(build_chunks(product))
    return chunks


def batched(items: list[Chunk], size: int) -> Iterable[list[Chunk]]:
    """按固定大小分批，降低 embedding 和 upsert 的单批压力。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def upsert_chunks(collection: Any, chunks: list[Chunk], batch_size: int) -> None:
    """分批计算向量并写入 Chroma。"""
    for chunk_batch in batched(chunks, batch_size):
        collection.upsert(
            ids=[chunk.id for chunk in chunk_batch],
            documents=[chunk.document for chunk in chunk_batch],
            metadatas=[chunk.metadata for chunk in chunk_batch],
        )


def print_stats(chunks: list[Chunk]) -> None:
    """打印索引组成，用于构建后的快速检查。"""
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
    """解析离线索引构建命令的参数。"""
    parser = argparse.ArgumentParser(description="Build the Chroma RAG index.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--persist-dir", type=Path, default=DEFAULT_PERSIST_DIR)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


def main() -> None:
    """从商品 JSON 文件构建 Chroma 索引。"""
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
