from __future__ import annotations

"""Build ``compare_index.json`` offline.

For each product, an LLM extracts concise values for fixed category dimensions from
descriptions, FAQ entries, and specifications. Runtime comparison then becomes a fast,
stable, reproducible lookup with no LLM call. Rebuild after data or dimension changes.

Usage:
    cd backend && python -m rag.build_compare_index            # 增量（已存在的跳过）
    cd backend && python -m rag.build_compare_index --force    # 全量重建
"""

import argparse
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from agent.comparison import (
    COMPARE_INDEX_PATH,
    all_dimensions_for_category,
    product_brief,
)
from llm.client import get_client, get_model_id
from store.product_store import ProductDetail, ProductStore


logger = logging.getLogger(__name__)


def _extract_schema(dimensions: list[str]) -> dict[str, Any]:
    """Build the function-calling schema for dimension values and audience tagline."""
    dim_lines = "\n".join(f"  - {d}" for d in dimensions)
    properties: dict[str, Any] = {
        dim: {
            "type": "string",
            "description": f"维度「{dim}」的简短描述（≤20字，依据资料，没提到填「—」）",
        }
        for dim in dimensions
    }
    properties["tagline"] = {
        "type": "string",
        "description": "一句『适合人群/定位』短语，≤16字，如『追求极致性能的科技爱好者』",
    }
    return {
        "type": "function",
        "function": {
            "name": "emit_product_dimensions",
            "description": "按固定维度抽取该商品的对比值与适合人群。维度：\n" + dim_lines,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": dimensions + ["tagline"],
            },
        },
    }


def _extract_one(detail: ProductDetail, timeout: float) -> dict[str, Any]:
    """Extract all dimension values and the audience tagline for one product."""
    dimensions = all_dimensions_for_category(detail.category)
    schema = _extract_schema(dimensions)
    system = (
        "你是电商导购的商品资料分析助手。请阅读商品资料，"
        "为给定的每个维度抽取一句简短客观的描述，并给出一句『适合人群』短语。"
        "必须调用 emit_product_dimensions 函数。只依据资料，不要编造；"
        "资料没提到的维度填「—」。"
    )
    user = f"商品资料：\n{product_brief(detail)}\n\n请抽取上述维度的值。"

    client = get_client()
    response = client.chat.completions.create(
        model=get_model_id(),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=[schema],
        tool_choice={"type": "function", "function": {"name": "emit_product_dimensions"}},
        temperature=0.0,
        timeout=timeout,
    )
    message = response.choices[0].message
    if not message.tool_calls:
        raise ValueError("The LLM did not call emit_product_dimensions")
    args = json.loads(message.tool_calls[0].function.arguments)

    dims = {d: str(args.get(d, "—")).strip() or "—" for d in dimensions}
    tagline = str(args.get("tagline", "")).strip()
    return {"dims": dims, "tagline": tagline}


def _load_existing(path: Path) -> dict[str, Any]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def build_index(
    out_path: Path = COMPARE_INDEX_PATH,
    force: bool = False,
    max_workers: int = 6,
) -> dict[str, Any]:
    """Build or incrementally complete the comparison index for active products."""
    store = ProductStore()
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT product_id FROM products WHERE status = 'active' ORDER BY product_id"
        ).fetchall()
    product_ids = [r["product_id"] for r in rows]

    existing = {} if force else _load_existing(out_path)
    todo = [pid for pid in product_ids if pid not in existing]
    timeout = float(os.getenv("ARK_TIMEOUT", "30"))
    logger.info(
        "Comparison index: %d products, %d existing, %d to extract",
        len(product_ids), len(existing), len(todo),
    )

    def _one(pid: str) -> tuple[str, dict[str, Any] | None]:
        detail = store.get_product_detail(pid)
        if detail is None:
            return pid, None
        try:
            return pid, _extract_one(detail, timeout)
        except Exception as exc:  # noqa: BLE001 - one product must not stop the batch
            logger.warning("Dimension extraction failed for product %s: %r", pid, exc)
            return pid, None

    result = dict(existing)
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for pid, entry in executor.map(_one, todo):
                if entry is not None:
                    result[pid] = entry

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("Comparison index written to %s with %d products", out_path, len(result))
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Build the product comparison index")
    parser.add_argument("--force", action="store_true", help="Ignore existing entries and rebuild all")
    parser.add_argument("--workers", type=int, default=6, help="Number of concurrent extraction workers")
    args = parser.parse_args()
    build_index(force=args.force, max_workers=args.workers)


if __name__ == "__main__":
    main()
