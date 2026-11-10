from __future__ import annotations

"""商品详情 / 批量查询 REST 端点。

挂载在 main.py 里的 FastAPI app 上：
    app.include_router(products.router)

端点：
    GET /products/{id}        单品详情
    GET /products?ids=a,b,c   批量查询
    GET /images/{id}          商品主图（直接从仓库 data/ 目录读文件）

商品图：原来挂在火山 TOS 的 bucket 已不存在（全部 404），现在由后端自己提供
``/images/{product_id}``，imageURL 按请求的 Host 拼成绝对地址；需要走 CDN /
公网域名时设 ``PRODUCT_IMAGE_BASE_URL``。manifest / 库里的远程 URL 只在本地
没有图片文件时才作为兜底。

数据源：SQLite ProductStore（backend/store/product_store.py）。
"""

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from catalog_config import AMAZON_MODE
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
DATA_DIR = PROJECT_ROOT / "data"
_IMAGE_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

router = APIRouter()
store = ProductStore(DEFAULT_DB_PATH)


def load_image_manifest() -> dict[str, str]:
    if not IMAGE_MANIFEST_PATH.exists():
        return {}
    with IMAGE_MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        parsed = json.load(handle)
    return {str(key): str(value) for key, value in parsed.items() if key and value}


image_manifest = load_image_manifest()


def local_image_file(detail: ProductDetail) -> Path | None:
    """商品主图在仓库 data/ 下的文件；没有或路径越界返回 None。"""
    rel = (getattr(detail, "image_path", None) or "").strip()
    if not rel:
        return None
    path = (DATA_DIR / rel).resolve()
    try:
        path.relative_to(DATA_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def image_base_url(request: Request | None) -> str | None:
    """拼 imageURL 用的前缀：优先 PRODUCT_IMAGE_BASE_URL（CDN / 公网域名），否则用本次请求的 Host。"""
    configured = (os.getenv("PRODUCT_IMAGE_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    if request is None:
        return None
    return str(request.base_url).rstrip("/")


def absolutize_image_urls(payload: Any, image_base: str | None) -> Any:
    """把 Agent 产出的结构化 payload 里的商品图地址改成后端自己的 /images/{id}。

    compare 工具（comparison.products[].image_url）等是在没有 request 的 Agent 层生成的，
    只能拿到 manifest / 库里的远程 URL；这里在 API 出口统一改写。就地修改并返回同一对象。
    """
    if not image_base or not isinstance(payload, dict):
        return payload

    def _rewrite(item: dict[str, Any]) -> None:
        pid = item.get("product_id") or item.get("productId") or item.get("productID")
        if not pid:
            return
        detail = store.get_product_detail(str(pid))
        if detail is not None and local_image_file(detail) is not None:
            item["image_url"] = f"{image_base}/images/{pid}"

    comparison = payload.get("comparison")
    if isinstance(comparison, dict):
        for item in comparison.get("products") or []:
            if isinstance(item, dict):
                _rewrite(item)
    for item in payload.get("products") or []:
        if isinstance(item, dict) and "image_url" in item:
            _rewrite(item)
    return payload


@router.get("/images/{product_id}")
def get_product_image(product_id: str) -> FileResponse:
    detail = store.get_product_detail(product_id)
    path = local_image_file(detail) if detail is not None else None
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=error_payload(code="IMAGE_NOT_FOUND", message="Product image not found", retryable=False),
        )
    media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/products/search")
def search_products(request: Request, q: str = Query(..., min_length=1, max_length=300),
                    limit: int = Query(10, ge=1, le=24)) -> dict[str, Any]:
    from search.search_service import get_search_service

    result = get_search_service().search(q, top_k_products=limit)
    details = (store.get_product_detail(hit.product_id) for hit in result.hits)
    return {
        "requestID": "catalog_search",
        "products": [product_payload(detail, image_base_url(request)) for detail in details if detail],
        "needsClarification": result.parsed.needs_clarification,
    }


@router.get("/products/{product_id}")
def get_product(product_id: str, request: Request) -> dict[str, Any]:
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
    return product_payload(detail, image_base=image_base_url(request))


@router.get("/products")
def get_products(
    request: Request,
    ids: str = Query(..., description="Comma-separated product ids"),
) -> dict[str, Any]:
    product_ids = [product_id.strip() for product_id in ids.split(",") if product_id.strip()]
    products = []
    base = image_base_url(request)
    for product_id in product_ids:
        detail = store.get_product_detail(product_id)
        if detail is not None:
            products.append(product_payload(detail, image_base=base))
    return {
        "requestID": "products_by_id",
        "products": products,
    }


@router.post("/products/{product_id}/pitch")
def product_pitch(product_id: str) -> dict[str, str]:
    """Compose one price-free recommendation description for a product."""
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
    if detail.catalog_source is not None:
        return {"description": detail.marketing_description[:300]}
    from agent.composer import AnswerComposer
    from agent.session import AgentSession
    from agent.tools.base import ToolResult

    tool_result = ToolResult(
        tool_name="recommend",
        payload={"query": detail.title, "products": [card]},
        composer_hint=(
            "这是商品详情页的推荐理由。"
            "JSON 的 items 只含这一条商品；description 写 1-2 句场景化卖点，"
            "【禁止出现任何价格、¥、元、起等价格表述】；followup 返回空数组。"
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


def resolve_image_url(detail: ProductDetail, image_base: str | None = None) -> str | None:
    """本地有图就指向后端自己的 /images/{id}；没有才退回 manifest / 库里的远程 URL。"""
    if image_base and local_image_file(detail) is not None:
        return f"{image_base}/images/{detail.product_id}"
    return image_manifest.get(detail.product_id) or detail.image_url


MAX_DETAIL_FAQS = 5


def product_payload(detail: ProductDetail, image_base: str | None = None) -> dict[str, Any]:
    image_url = resolve_image_url(detail, image_base)
    specs = specifications_payload(detail.skus)
    payload = {
        "productID": detail.product_id,
        "title": detail.title,
        "category": detail.category,
        "brand": detail.brand,
        "imageURL": image_url,
        "detailURL": None,
        "price": money(detail.price_range.min_price, price_display(detail.price_range)),
        "priceRange": {
            "min": money(detail.price_range.min_price),
            "max": money(detail.price_range.max_price),
            "skuCount": detail.price_range.sku_count,
        },
        "originalPrice": None,
        "availability": availability(detail.skus),
        "summary": detail.marketing_description or None,
        "tags": [value for value in [detail.category, detail.sub_category, detail.brand] if value],
        "specifications": specs,
        "skus": [sku_payload(sku) for sku in detail.skus],
        "reviews": [review_payload(review) for review in detail.reviews],
        "evidence": evidence_payload(detail),
        "faqs": [{"question": faq.question, "answer": faq.answer} for faq in detail.faqs[:MAX_DETAIL_FAQS]],
        "updatedAt": None,
    }
    if detail.catalog_source is not None:
        payload.update(priceType="reference", catalogSource=detail.catalog_source)
    return payload


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
                "title": f"{review.nickname} · {review.rating}/5" if AMAZON_MODE else f"{review.nickname} · {review.rating} 星",
                "snippet": review.content,
                "score": None,
                "updatedAt": None,
            }
        )
    return evidence


def money(amount: float, display: str | None = None) -> dict[str, Any]:
    return {
        "currency": "USD" if AMAZON_MODE else "CNY",
        "amountMinor": int(round(amount * 100)),
        "display": display or (f"${amount:,.2f}" if AMAZON_MODE else f"¥{amount:g}"),
    }


def availability(skus: list[ProductSku]) -> str:
    if not skus:
        return "unknown"
    if any(sku.stock_qty > 10 for sku in skus):
        return "inStock"
    if any(sku.stock_qty > 0 for sku in skus):
        return "lowStock"
    return "outOfStock"
