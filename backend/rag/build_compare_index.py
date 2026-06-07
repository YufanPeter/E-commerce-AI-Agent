from __future__ import annotations

"""离线构建商品对比索引（compare_index.json）。

为每个商品，按其类目的固定对比维度，用 LLM 从 marketing_description / official_faq /
规格里抽取每个维度的简短值，并生成一句"适合人群"短语。结果落盘后，运行时对比就是
纯查表、零 LLM——快、稳、可复现。只需在数据/维度定义变化时重跑一次。

运行：
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
    """function-calling schema：为固定维度逐一填值 + 一句适合人群短语。"""
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
    """对单个商品抽取所有维度值 + tagline。"""
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
        raise ValueError("LLM 未调用 emit_product_dimensions")
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
    """为全部在售商品构建（或增量补全）对比索引并落盘。"""
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
        "对比索引：共 %d 个商品，已存在 %d，本次需抽取 %d",
        len(product_ids), len(existing), len(todo),
    )

    def _one(pid: str) -> tuple[str, dict[str, Any] | None]:
        detail = store.get_product_detail(pid)
        if detail is None:
            return pid, None
        try:
            return pid, _extract_one(detail, timeout)
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            logger.warning("商品 %s 维度抽取失败：%r", pid, exc)
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
    logger.info("对比索引已写入 %s（共 %d 个商品）", out_path, len(result))
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="构建商品对比索引")
    parser.add_argument("--force", action="store_true", help="忽略已有索引，全量重建")
    parser.add_argument("--workers", type=int, default=6, help="并发抽取线程数")
    args = parser.parse_args()
    build_index(force=args.force, max_workers=args.workers)


if __name__ == "__main__":
    main()
