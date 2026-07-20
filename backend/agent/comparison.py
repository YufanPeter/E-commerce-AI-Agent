from __future__ import annotations

"""Core product-comparison builder shared by the tool and REST endpoint.

The result is a render-ready comparison-table structure:
    {
      "title": "对比：A vs B",
      "products": [{product_id,title,brand,price,image_url}, ...],
      "rows": [{"label": "价格", "values": ["¥8999","¥5999"], "highlight": 1}, ...],
      "recommendation": "什么人选哪个的一句话建议"
    }

Design: offline extraction plus runtime lookup, with no runtime LLM call.
- Dimension values such as battery life, performance, ingredients, and material live
  in unstructured product text rather than dedicated fields. ``build_compare_index``
  extracts fixed dimensions and a short audience tagline into ``compare_index.json``.
- At runtime, ``build_comparison`` performs lookups only. The price row comes from
  source data and the remaining rows come from the index, making results fast,
  stable, reproducible, and free of inference cost.
- ``_CATEGORY_DIMENSIONS`` fixes the dimension set for each category. Text dimensions
  deliberately have no winner; only the deterministic price row is highlighted.

If the offline index is missing, the response degrades gracefully to the price row.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from store.product_store import ProductDetail, price_display


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_IMAGE_MANIFEST_PATH = PROJECT_ROOT / "backend" / "cdn" / "image_manifest.json"
COMPARE_INDEX_PATH = PROJECT_ROOT / "backend" / "storage" / "compare_index.json"


def _load_image_manifest() -> dict[str, str]:
    if not _IMAGE_MANIFEST_PATH.exists():
        return {}
    with _IMAGE_MANIFEST_PATH.open("r", encoding="utf-8") as f:
        parsed = json.load(f)
    return {str(k): str(v) for k, v in parsed.items() if k and v}


_IMAGE_MANIFEST = _load_image_manifest()


# Fixed, ordered comparison dimensions per category. Products in the same category
# always use the same dimensions for stable and reproducible output. Price is generated
# separately by the deterministic ``_price_row`` helper.
#
# The dimensions follow the fields present in the source catalog, avoiding empty rows:
# electronics use description/FAQ plus SKU storage; beauty uses description plus SKU
# volume; apparel uses description plus SKU size/color; food uses description plus SKU
# flavor/count.
_CATEGORY_DIMENSIONS: dict[str, list[str]] = {
    "数码电子": ["性能/芯片", "续航", "屏幕", "存储规格"],
    "美妆护肤": ["核心功效", "关键成分", "适用肤质", "容量规格"],
    "服饰运动": ["材质面料", "版型", "适用场景", "尺码颜色"],
    "食品生活": ["口味风味", "配料成分", "规格分量", "适用场景"],
}

# Generic dimensions for cross-category comparisons.
_GENERIC_DIMENSIONS = ["品牌定位", "核心卖点", "适用场景"]

# Placeholder for a missing value.
_EMPTY = "—"


def _dimensions_for(details: list[ProductDetail]) -> list[str]:
    """Select a fixed dimension set for the products being compared.

    Same-category products use category dimensions; cross-category products use the
    generic set. Returned labels become the final table row names.
    """
    categories = {d.category for d in details}
    if len(categories) == 1:
        return _CATEGORY_DIMENSIONS.get(next(iter(categories)), _GENERIC_DIMENSIONS)
    return _GENERIC_DIMENSIONS


def all_dimensions_for_category(category: str) -> list[str]:
    """Return all dimensions extracted offline for a category.

    The result combines category and generic dimensions while preserving order and
    removing duplicates, so cross-category comparisons still have populated rows.
    """
    base = _CATEGORY_DIMENSIONS.get(category, [])
    out = list(base)
    for d in _GENERIC_DIMENSIONS:
        if d not in out:
            out.append(d)
    return out


def product_brief(detail: ProductDetail) -> str:
    """Compress comparable product data for the offline extraction prompt."""
    parts = [
        f"标题：{detail.title}",
        f"品牌：{detail.brand}",
        f"类目：{detail.category} / {detail.sub_category or ''}",
        f"价格区间：{price_display(detail.price_range)}",
    ]
    if detail.skus:
        spec_bits: list[str] = []
        for sku in detail.skus[:8]:
            if sku.properties:
                spec_bits.append("、".join(f"{k}:{v}" for k, v in sku.properties.items()))
        if spec_bits:
            parts.append("规格示例：" + "；".join(spec_bits[:6]))
    if detail.marketing_description:
        parts.append("卖点描述：" + detail.marketing_description[:600])
    if detail.faqs:
        faq_txt = " ".join(f"Q:{f.question} A:{f.answer}" for f in detail.faqs[:5])
        parts.append("FAQ：" + faq_txt[:700])
    if detail.reviews:
        rev_txt = " ".join(r.content for r in detail.reviews[:4])
        parts.append("用户评价：" + rev_txt[:400])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Offline index: {product_id: {"dims": {dimension: value}, "tagline": audience}}
# ---------------------------------------------------------------------------

class CompareIndex:
    """Offline comparison-dimension index used for runtime lookups."""

    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self._data = data

    def __len__(self) -> int:
        return len(self._data)

    def has(self, product_id: str) -> bool:
        return product_id in self._data

    def value(self, product_id: str, dimension: str) -> str:
        """Return a pre-extracted dimension value or the missing-value marker."""
        entry = self._data.get(product_id) or {}
        dims = entry.get("dims") or {}
        val = dims.get(dimension)
        return str(val).strip() if val else _EMPTY

    def tagline(self, product_id: str) -> str:
        """Return the product's audience/positioning tagline, if available."""
        entry = self._data.get(product_id) or {}
        return str(entry.get("tagline") or "").strip()


def load_compare_index(path: Path = COMPARE_INDEX_PATH) -> CompareIndex:
    """Load the comparison index, returning an empty index when absent."""
    if not path.exists():
        logger.warning(
            "Comparison index %s is missing; comparisons will show only the price row. "
            "Run build_compare_index first.",
            path,
        )
        return CompareIndex({})
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info("Loaded comparison index for %d products", len(data))
    return CompareIndex(data)


@lru_cache(maxsize=1)
def get_compare_index() -> CompareIndex:
    """Return the process-wide index shared by tools and REST endpoints."""
    return load_compare_index()


# ---------------------------------------------------------------------------
# Runtime construction: lookups only, no LLM calls.
# ---------------------------------------------------------------------------

def _price_row(details: list[ProductDetail]) -> dict[str, Any]:
    """Build a deterministic price row and highlight the cheapest product."""
    values = [price_display(d.price_range) for d in details]
    min_idx = min(range(len(details)), key=lambda i: details[i].price_range.min_price)
    return {"label": "价格", "values": values, "highlight": min_idx}


def _product_header(detail: ProductDetail) -> dict[str, Any]:
    return {
        "product_id": detail.product_id,
        "title": detail.title,
        "brand": detail.brand,
        "price": detail.price_range.min_price,
        "image_url": _IMAGE_MANIFEST.get(detail.product_id) or detail.image_url,
    }


def build_comparison(
    details: list[ProductDetail],
    focus: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Build a comparison from a deterministic price row and indexed dimensions.

    At least two products are required. No LLM is called at runtime. ``focus`` and
    ``timeout`` remain for backward compatibility and are currently unused.
    """
    if len(details) < 2:
        raise ValueError("A comparison requires at least two products")

    index = get_compare_index()
    dimensions = _dimensions_for(details)

    rows: list[dict[str, Any]] = [_price_row(details)]
    for dim in dimensions:
        values = [index.value(d.product_id, dim) for d in details]
        # Do not force a winner for textual dimensions; only price is deterministic.
        rows.append({"label": dim, "values": values, "highlight": None})

    titles = " vs ".join(d.title[:16] for d in details)
    return {
        "title": f"对比：{titles}",
        "products": [_product_header(d) for d in details],
        "rows": rows,
        # Keep the field for compatibility while presenting objective rows only.
        "recommendation": "",
    }
