from __future__ import annotations

"""商品详情 / 批量查询 REST 端点。

挂载在 main.py 里的 FastAPI app 上：
    app.include_router(products.router)

端点：
    GET /products/{id}        单品详情
    GET /products?ids=a,b,c   批量查询

数据源：SQLite ProductStore（backend/store/product_store.py）。
"""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from store.product_store import (
    DEFAULT_DB_PATH,
    ProductDetail,
    ProductReview,
    ProductSku,
    ProductStore,
    price_display,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE_MANIFEST_PATH = PROJECT_ROOT / "backend" / "cdn" / "image_manifest.json"

router = APIRouter()
store = ProductStore(DEFAULT_DB_PATH)


def load_image_manifest() -> dict[str, str]:
    if not IMAGE_MANIFEST_PATH.exists():
        return {}
    with IMAGE_MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        parsed = json.load(handle)
    return {str(key): str(value) for key, value in parsed.items() if key and value}


image_manifest = load_image_manifest()


@router.get("/products/{product_id}")
def get_product(product_id: str) -> dict[str, Any]:
    detail = store.get_product_detail(product_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                code="PRODUCT_NOT_FOUND",
                message="Product not found",
                retryable=False,
            ),
        )
    return product_payload(detail)


@router.get("/products")
def get_products(ids: str = Query(..., description="Comma-separated product ids")) -> dict[str, Any]:
    product_ids = [product_id.strip() for product_id in ids.split(",") if product_id.strip()]
    products = []
    for product_id in product_ids:
        detail = store.get_product_detail(product_id)
        if detail is not None:
            products.append(product_payload(detail))
    return {
        "requestID": "products_by_id",
        "products": products,
    }


@router.post("/products/{product_id}/pitch")
def product_pitch(product_id: str) -> dict[str, str]:
    """单品推荐理由：走与导购 recommend 相同的 composer，只返回一条不含价格的 description。"""
    detail = store.get_product_detail(product_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=error_payload(
                code="PRODUCT_NOT_FOUND",
                message="Product not found",
                retryable=False,
            ),
        )

    card = {
        "product_id": detail.product_id,
        "title": detail.title,
        "brand": detail.brand,
        "category": detail.category,
        "sub_category": detail.sub_category,
        "price": detail.price_range.min_price,
        "price_display": price_display(detail.price_range),
    }
    from agent.composer import AnswerComposer
    from agent.session import AgentSession
    from agent.tools.base import ToolResult

    tool_result = ToolResult(
        tool_name="recommend",
        payload={"query": detail.title, "products": [card]},
        composer_hint=(
            "这是商品详情页的推荐理由。"
            "JSON 的 items 只含这一条商品；description 写 1-2 句场景化卖点，"
            "【禁止出现任何价格、¥、元、起等价格表述】；questions 返回空数组。"
        ),
    )
    narrative = AnswerComposer().compose(tool_result, AgentSession(), timeout=15.0)
    description = _extract_pitch_description(narrative, detail.product_id)
    return {"description": description}


def _extract_pitch_description(narrative: str, product_id: str) -> str:
    text = (narrative or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:240]
    items = data.get("items") or []
    if not items:
        return (data.get("opening") or "")[:240]
    for item in items:
        pid = item.get("productId") or item.get("product_id")
        if pid == product_id or len(items) == 1:
            return str(item.get("description") or "").strip()
    return str(items[0].get("description") or "").strip()


def error_payload(code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "traceID": None,
    }


def default_error_code(status_code: int) -> str:
    return f"API_HTTP_{status_code}"


def product_payload(detail: ProductDetail) -> dict[str, Any]:
    image_url = image_manifest.get(detail.product_id) or detail.image_url
    specs = specifications_payload(detail.skus)
    return {
        "productID": detail.product_id,
        "title": detail.title,
        "category": detail.category,
        "brand": detail.brand,
        "imageURL": image_url,
        "detailURL": None,
        "price": money(detail.price_range.min_price, price_display(detail.price_range)),
        "originalPrice": None,
        "availability": availability(detail.skus),
        "summary": detail.marketing_description or None,
        "tags": [value for value in [detail.category, detail.sub_category, detail.brand] if value],
        "specifications": specs,
        "skus": [sku_payload(sku) for sku in detail.skus],
        "reviews": [review_payload(review) for review in detail.reviews],
        "evidence": evidence_payload(detail),
        "updatedAt": None,
    }


def specifications_payload(skus: list[ProductSku]) -> list[dict[str, Any]]:
    option_sets: dict[str, set[str]] = {}
    for sku in skus:
        for name, value in sku.properties.items():
            option_sets.setdefault(str(name), set()).add(str(value))

    return [
        {
            "id": name,
            "name": name,
            "options": [
                {"id": f"{name}_{option}", "label": option, "isAvailable": True}
                for option in sorted(options)
            ],
        }
        for name, options in sorted(option_sets.items())
    ]


def sku_payload(sku: ProductSku) -> dict[str, Any]:
    return {
        "id": sku.sku_id,
        "selectedOptions": {str(key): str(value) for key, value in sku.properties.items()},
        "price": money(sku.price),
        "availability": "inStock" if sku.stock_qty > 0 else "outOfStock",
        "stockCount": sku.stock_qty,
    }


def review_payload(review: ProductReview) -> dict[str, Any]:
    return {
        "id": f"review_{review.source_index}",
        "nickname": review.nickname,
        "rating": review.rating,
        "content": review.content,
        "polarity": review.polarity,
    }


def evidence_payload(detail: ProductDetail) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    if detail.marketing_description:
        evidence.append(
            {
                "id": f"{detail.product_id}_marketing",
                "sourceType": "productDetail",
                "title": detail.title,
                "snippet": detail.marketing_description,
                "score": 1,
                "updatedAt": None,
            }
        )

    for faq in detail.faqs[:2]:
        evidence.append(
            {
                "id": f"{detail.product_id}_faq_{faq.source_index}",
                "sourceType": "productDetail",
                "title": faq.question,
                "snippet": faq.answer,
                "score": None,
                "updatedAt": None,
            }
        )

    for review in detail.reviews:
        evidence.append(
            {
                "id": f"{detail.product_id}_review_{review.source_index}",
                "sourceType": "userReview",
                "title": f"{review.nickname} · {review.rating} 星",
                "snippet": review.content,
                "score": None,
                "updatedAt": None,
            }
        )
    return evidence


def money(amount: float, display: str | None = None) -> dict[str, Any]:
    return {
        "currency": "CNY",
        "amountMinor": int(round(amount * 100)),
        "display": display or f"¥{amount:g}",
    }


def availability(skus: list[ProductSku]) -> str:
    if not skus:
        return "unknown"
    if any(sku.stock_qty > 10 for sku in skus):
        return "inStock"
    if any(sku.stock_qty > 0 for sku in skus):
        return "lowStock"
    return "outOfStock"
