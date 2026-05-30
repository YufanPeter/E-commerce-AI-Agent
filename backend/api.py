from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from search.search_service import SearchResult, get_search_service
from store.product_store import (
    DEFAULT_DB_PATH,
    ProductDetail,
    ProductSku,
    ProductStore,
    price_display,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMAGE_MANIFEST_PATH = PROJECT_ROOT / "backend" / "cdn" / "image_manifest.json"

app = FastAPI(title="E-commerce AI Agent API")
store = ProductStore(DEFAULT_DB_PATH)
image_manifest: dict[str, str] = {}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Any, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        detail = exc.detail
    else:
        detail = error_payload(
            code=default_error_code(exc.status_code),
            message=str(exc.detail),
            retryable=exc.status_code >= 500,
        )
    return JSONResponse(status_code=exc.status_code, content=detail)


@app.get("/products/{product_id}")
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


@app.get("/products")
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


@app.get("/search")
def search_products(
    q: str = Query(..., description="自然语言检索 query，例如 ‘300以上的Nike跑鞋’"),
    limit: int = Query(10, ge=1, le=50, description="返回商品数上限"),
) -> dict[str, Any]:
    """RAG 端到端检索。命中走严格匹配；零命中时按品类放宽阶梯降级，
    通过 matchType / fallbackReason 让前端区分严格结果与放宽结果。"""
    try:
        result = get_search_service().search(q, top_k_products=limit)
    except Exception as exc:  # 上游 LLM / 检索异常统一收敛成可重试错误
        raise HTTPException(
            status_code=502,
            detail=error_payload(
                code="SEARCH_UPSTREAM_FAILED",
                message="检索服务暂时不可用，请稍后重试。",
                retryable=True,
            ),
        ) from exc
    return search_payload(result)


def load_image_manifest() -> dict[str, str]:
    if not IMAGE_MANIFEST_PATH.exists():
        return {}
    with IMAGE_MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        parsed = json.load(handle)
    return {str(key): str(value) for key, value in parsed.items() if key and value}


image_manifest = load_image_manifest()


def error_payload(code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "traceID": None,
    }


def default_error_code(status_code: int) -> str:
    return f"API_HTTP_{status_code}"


def search_payload(result: SearchResult) -> dict[str, Any]:
    """把 SearchResult 包成检索响应：商品列表 + 严格/降级标记。

    matchType=strict 时 products 是完全匹配；matchType=fallback 时是放宽后的
    兜底结果，fallbackReason 说明放宽了什么。二者互斥，不会出现在同一次响应里。
    """
    products: list[dict[str, Any]] = []
    for hit in result.hits:
        detail = store.get_product_detail(hit.product_id)
        if detail is None:
            continue
        payload = product_payload(detail)
        payload["score"] = hit.score
        products.append(payload)

    return {
        "requestID": "search",
        "query": result.parsed.original_query,
        "matchType": "fallback" if result.is_fallback else "strict",
        "fallbackReason": result.fallback_reason,
        "relaxedFilters": result.relaxed_filters,
        "products": products,
        "parsed": result.parsed.to_dict(),
    }


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
        "tags": [value for value in [detail.category, detail.sub_category, detail.brand] if value],
        "specifications": specs,
        "skus": [sku_payload(sku) for sku in detail.skus],
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

    for review in detail.reviews[:2]:
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
