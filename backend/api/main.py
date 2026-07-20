from __future__ import annotations

"""FastAPI application exposing the agent over HTTP and SSE.

Endpoints:
    ``POST /chat`` returns one JSON response.
    ``POST /chat/stream`` streams meta, tool result, token, done, or error events.
    ``GET /health`` reports health.

Sessions are identified by a client-provided UUID and persisted through the session store.
"""

import asyncio
import json
import logging
import os
import threading
from typing import Any

# Configure the environment before importing Chroma or sentence-transformers.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_IMPL", "none")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent.memory_extractor import extract_preference_updates
from api import products as products_router
from store.cart_store import CartStore
from store.session_store import SqliteSessionStore
from store.user_memory_store import UserMemoryStore, UserPreference


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request and response models.
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    query: str = Field(..., description="Natural-language user query")
    session_id: str | None = Field(None, description="Session ID; omitted to create one")
    user_id: str | None = Field(None, description="User ID for cross-session preferences")
    image_base64: str | None = Field(
        None,
        description="Image-search base64 data, with or without a data-URI prefix",
    )
    image_url: str | None = Field(
        None, description="Remote image-search URL; mutually exclusive with image_base64"
    )


class ChatResponse(BaseModel):
    session_id: str
    decision: dict[str, Any]
    tool_result: dict[str, Any]
    narrative: str
    trace: dict[str, Any]


class CompareRequest(BaseModel):
    product_ids: list[str] = Field(..., description="Product IDs to compare")
    focus: str | None = Field(None, description="Optional comparison focus")


class TitleRequest(BaseModel):
    user_text: str = Field(..., description="First user message")
    assistant_text: str | None = Field(None, description="Optional first assistant reply")


class CartMutationRequest(BaseModel):
    action: str = Field(..., description="add/updateQuantity/updateSpecification/remove")
    productID: str | None = None
    skuID: str | None = None
    cartItemID: str | None = None
    selectedOptions: dict[str, str] = Field(default_factory=dict)
    quantity: int | None = None
    selectedCartItemIDs: list[str] = Field(default_factory=list)


class PreferenceRequest(BaseModel):
    personalization_enabled: bool = True
    budget_min: float | None = None
    budget_max: float | None = None
    price_tier: str | None = None
    favorite_categories: list[str] = Field(default_factory=list)
    brand_include: list[str] = Field(default_factory=list)
    brand_exclude: list[str] = Field(default_factory=list)
    preference_keywords: list[str] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
    category_specific: dict[str, dict[str, Any]] = Field(default_factory=dict)
    preference_note: str | None = None
    notes: str | None = None


class PreferenceUndoRequest(BaseModel):
    undo_token: str = Field(..., description="Undo token returned by a memory_update event")


# ---------------------------------------------------------------------------
# Application and session management.
# ---------------------------------------------------------------------------

app = FastAPI(title="E-commerce AI Agent", version="0.2.0")

# Product detail and batch-query endpoints.
app.include_router(products_router.router)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Normalize HTTP errors while preserving structured detail dictionaries."""
    if isinstance(exc.detail, dict):
        detail = exc.detail
    else:
        detail = products_router.error_payload(
            code=products_router.default_error_code(exc.status_code),
            message=str(exc.detail),
            retryable=exc.status_code >= 500,
        )
    return JSONResponse(status_code=exc.status_code, content=detail)


_sessions = SqliteSessionStore()
_user_memory = UserMemoryStore()
_agent: Any | None = None  # Avoid heavy search/model initialization during import.
_agent_lock = threading.Lock()
_warmup_status: dict[str, str] = {
    "status": "disabled",
    "message": "Startup warmup is disabled.",
}


def _get_agent() -> Any:
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is None:
            logger.info("Initializing Agent (will load search service & embeddings)")
            from agent.orchestrator import Agent

            _agent = Agent()
    return _agent


def _attach_user_memory(session: Any, user_id: str | None) -> None:
    """Attach long-lived user preference to this request-scoped session object."""
    if not user_id:
        session.user_id = None
        session.user_profile = {}
        return
    clean_user_id = user_id.strip()
    preference = _user_memory.get_preference(clean_user_id)
    session.user_id = clean_user_id
    session.user_profile = preference.to_dict()


def _apply_realtime_preference_updates(query: str, session: Any) -> list[dict[str, Any]]:
    user_id = getattr(session, "user_id", None)
    if not user_id:
        return []
    updates = extract_preference_updates(query, user_id, getattr(session, "session_id", None))
    events: list[dict[str, Any]] = []
    for update in updates:
        event = _user_memory.apply_update(user_id, update)
        if event is not None:
            events.append(event)
    if events:
        session.user_profile = _user_memory.get_preference(user_id).to_dict()
    return events


@app.on_event("startup")
def _warmup() -> None:
    """Warm the agent, search service, embeddings, and reranker in a background thread.

    Without warmup, the first SSE request may trigger slow lazy initialization and pause
    after the tool status event.

    Background execution lets Uvicorn bind immediately and keeps ``/health`` available.
    A request arriving before completion follows the ordinary lazy-load path.
    """
    if os.getenv("BACKEND_WARMUP", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        _warmup_status.update(
            status="disabled",
            message="Startup warmup is disabled. Set BACKEND_WARMUP=1 to preload Agent/SearchService.",
        )
        logger.info("Startup warmup skipped; set BACKEND_WARMUP=1 to preload Agent/SearchService.")
        return

    _warmup_status.update(status="warming", message="Preloading Agent/SearchService in background.")

    def _run() -> None:
        try:
            logger.info("Warmup: initializing agent and loading models…")
            from search.search_service import get_search_service

            _get_agent()
            svc = get_search_service()
            # Run one real retrieval to initialize embeddings and the reranking client.
            svc.search("预热查询", top_k_products=1)
            _warmup_status.update(status="ready", message="Agent/SearchService warmup completed.")
            logger.info("Warmup done: models are hot.")
        except Exception as exc:  # noqa: BLE001 - warmup must not prevent startup
            _warmup_status.update(status="failed", message=f"Warmup failed: {type(exc).__name__}: {exc}")
        logger.exception("Warmup failed; the service remains available but the first query will be slower")

    threading.Thread(target=_run, name="warmup", daemon=True).start()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/warmup")
def warmup_status() -> dict[str, str]:
    return dict(_warmup_status)


@app.get("/preferences/{user_id}")
def get_preferences(user_id: str) -> dict[str, Any]:
    """Read cross-session core preference memory for a user."""
    if not user_id.strip():
        raise HTTPException(status_code=400, detail="user_id 不能为空")
    return _user_memory.get_preference(user_id.strip()).to_dict()


@app.put("/preferences/{user_id}")
def put_preferences(user_id: str, req: PreferenceRequest) -> dict[str, Any]:
    """Replace cross-session core preference memory for a user."""
    clean_user_id = user_id.strip()
    if not clean_user_id:
        raise HTTPException(status_code=400, detail="user_id 不能为空")
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    preference = UserPreference.from_dict(clean_user_id, data)
    return _user_memory.put_preference(preference).to_dict()


@app.post("/preferences/{user_id}/undo")
def undo_preference(user_id: str, req: PreferenceUndoRequest) -> dict[str, Any]:
    """Undo a recent automatic preference write."""
    clean_user_id = user_id.strip()
    if not clean_user_id:
        raise HTTPException(status_code=400, detail="user_id 不能为空")
    result = _user_memory.undo_update(clean_user_id, req.undo_token.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="撤销 token 不存在或已使用")
    return result


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    session = _sessions.get_or_create(req.session_id)
    _attach_user_memory(session, req.user_id)
    resp = _get_agent().handle_turn(req.query, session)
    memory_updates = _apply_realtime_preference_updates(req.query, session)
    if memory_updates:
        resp.trace["memory_updates"] = memory_updates
    _sessions.save(session)  # Persist the turn so context survives a restart.
    return ChatResponse(
        session_id=session.session_id,
        decision=resp.decision.to_dict(),
        tool_result=resp.tool_result.to_dict(),
        narrative=resp.narrative,
        trace=resp.trace,
    )


def _resolve_image(req: ChatRequest) -> str | None:
    """Normalize a request image to an ``image_url``-compatible value.

    Prefer a remote URL; otherwise use base64 and add a data-URI prefix when missing.
    Return ``None`` when no image exists so the text path is used.
    """
    if req.image_url and req.image_url.strip():
        return req.image_url.strip()
    if req.image_base64 and req.image_base64.strip():
        b64 = req.image_base64.strip()
        if b64.startswith("data:"):
            return b64
        return f"data:image/jpeg;base64,{b64}"
    return None


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Stream one agent turn over Server-Sent Events.

    Each event ends with ``\\n\\n``:
        event: <session|status|meta|tool_result|token|done|error>
        data: <json>

    Event order:
        session → status(routing) → meta → status(tool) → tool_result
                → status(compose)? → token* → done

    ``status.data.message`` is user-facing progress copy; ``meta`` and ``tool_result``
    contain structured data.

    Clients may consume the stream with ``EventSource`` or a streaming HTTP client.
    """
    session = _sessions.get_or_create(req.session_id)
    _attach_user_memory(session, req.user_id)
    agent = _get_agent()
    image = _resolve_image(req)

    def _format_sse(event: str, payload: Any) -> str:
        data = json.dumps(payload, ensure_ascii=False)
        return f"event: {event}\ndata: {data}\n\n"

    def _generator():
        # Send the session ID first so the client can persist it.
        yield _format_sse("session", {"session_id": session.session_id})
        try:
            # An image selects visual search; query becomes optional accompanying text.
            if image:
                stream = agent.handle_image_turn_stream(
                    image, session, hint_text=req.query
                )
            else:
                stream = agent.handle_turn_stream(req.query, session)
            for ev in stream:
                # Emit preference updates before done for the client banner.
                if ev["type"] == "done":
                    for memory_event in _apply_realtime_preference_updates(req.query, session):
                        yield _format_sse("memory_update", memory_event)
                yield _format_sse(ev["type"], ev["data"])
        except Exception as exc:  # noqa: BLE001 - every stream failure reaches the client
            logger.exception("Stream pipeline crashed")
            yield _format_sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            # Persist once generator-managed history and working memory are final.
            _sessions.save(session)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        # Disable proxy buffering so tokens are delivered immediately.
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(_generator(), media_type="text/event-stream", headers=headers)


@app.post("/sessions/{session_id}/reset")
def reset_session(session_id: str) -> dict[str, str]:
    _sessions.reset(session_id)
    return {"status": "reset", "session_id": session_id}


@app.post("/cart/reset")
def reset_cart() -> dict[str, Any]:
    """Clear the cart for the single demonstration user.

    Cart state is persisted independently of sessions. Use this only for manual reset or
    debugging; clients should load existing cart state at startup.
    """
    removed = CartStore().clear()
    return {"status": "cleared", "removed": removed}


def _cart_response(store: CartStore) -> dict[str, Any]:
    lines = store.list_items()
    return {
        "cart": {
            "lines": [line.to_dict() for line in lines],
            "item_count": sum(line.quantity for line in lines),
            "total": round(sum(line.subtotal for line in lines), 2),
        }
    }


def _cart_item_id(value: str | None) -> int:
    try:
        return int(str(value or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="cartItemID 无效") from exc


@app.post("/cart/mutate")
def mutate_cart(req: CartMutationRequest) -> dict[str, Any]:
    """Apply a deterministic cart mutation and return the latest snapshot."""
    store = CartStore()
    action = req.action

    if action == "add":
        if not req.productID:
            raise HTTPException(status_code=400, detail="productID 不能为空")
        store.add_product(
            req.productID,
            sku_id=req.skuID,
            quantity=req.quantity or 1,
        )
    elif action == "remove":
        removed = store.remove_item(_cart_item_id(req.cartItemID))
        if not removed:
            raise HTTPException(status_code=404, detail="购物车商品不存在")
    elif action == "updateQuantity":
        if req.quantity is None:
            raise HTTPException(status_code=400, detail="quantity 不能为空")
        store.set_quantity(_cart_item_id(req.cartItemID), req.quantity)
    elif action == "updateSpecification":
        if not req.productID or not req.skuID:
            raise HTTPException(status_code=400, detail="productID 和 skuID 不能为空")
        old_id = _cart_item_id(req.cartItemID)
        current = next(
            (line for line in store.list_items() if line.cart_item_id == old_id),
            None,
        )
        if current is None:
            raise HTTPException(status_code=404, detail="购物车商品不存在")
        quantity = req.quantity or current.quantity
        store.remove_item(old_id)
        store.add_product(req.productID, sku_id=req.skuID, quantity=quantity)
    else:
        raise HTTPException(status_code=400, detail="不支持的购物车操作")

    return _cart_response(store)


@app.get("/cart")
def get_cart() -> dict[str, Any]:
    """Return the current cart snapshot for the demonstration user.

    Startup restoration keeps client state consistent with SQLite. The response matches
    the cart snapshot emitted by SSE so clients can share one parser.
    """
    return _cart_response(CartStore())


@app.post("/compare")
def compare(req: CompareRequest) -> dict[str, Any]:
    """Return a structured comparison for two product IDs.

    The conversational tool and comparison screen share ``build_comparison``.
    """
    from agent.comparison import build_comparison
    from store.product_store import ProductStore

    ids = [pid.strip() for pid in req.product_ids if pid and pid.strip()]
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="对比至少需要 2 个商品 id")
    ids = ids[:2]  # Keep comparison output to two products.

    store = ProductStore()
    details = []
    for pid in ids:
        detail = store.get_product_detail(pid)
        if detail is not None:
            details.append(detail)
    if len(details) < 2:
        raise HTTPException(status_code=404, detail="有效商品不足 2 个，无法对比")

    return build_comparison(details, focus=(req.focus or ""))


@app.post("/title")
def generate_title(req: TitleRequest) -> dict[str, str]:
    """Generate a short title from the first conversation turn.

    The client calls this asynchronously and falls back to a local truncated title on
    failure or timeout.
    """
    user_text = (req.user_text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text 不能为空")

    from llm.client import get_client, get_model_id

    system = (
        "你是对话标题生成器。根据用户的购物需求，生成一个简洁的中文标题，"
        "用于历史记录列表展示。要求：① 4-8 个字；② 概括核心需求（品类+关键属性），"
        "如「油皮洗面奶」「平价蓝牙耳机」「三亚出行搭配」；③ 只输出标题本身，"
        "不要引号、标点、解释。"
    )
    convo = f"用户：{user_text}"
    if req.assistant_text and req.assistant_text.strip():
        convo += f"\n助手：{req.assistant_text.strip()[:200]}"

    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=get_model_id(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": convo},
            ],
            temperature=0.3,
            max_tokens=20,
            timeout=float(os.getenv("ARK_TITLE_TIMEOUT", "8")),
        )
        title = (resp.choices[0].message.content or "").strip()
        title = title.strip("「」\"'。.，、 \n\t")[:12]
    except Exception as exc:  # noqa: BLE001 - return empty so the client can fall back
        logger.warning("Title generation failed: %r", exc)
        title = ""

    if not title:
        title = user_text[:12]
    return {"title": title}


@app.get("/suggestions")
def get_suggestions() -> dict[str, Any]:
    """Return empty-state home categories and dynamic popular searches.

    Suggestions are sampled from real brand/subcategory combinations and rotate on each
    request, ensuring each term maps to inventory.
    """
    import random

    from store.product_store import ProductStore

    store = ProductStore()
    with store.connect() as conn:
        cats = [
            r["category"]
            for r in conn.execute(
                "SELECT category, COUNT(*) n FROM products WHERE status='active' "
                "GROUP BY category ORDER BY n DESC"
            ).fetchall()
        ]
        # Real brand/subcategory combinations guaranteed to map to products.
        pairs = [
            (r["brand"], r["sub_category"])
            for r in conn.execute(
                "SELECT DISTINCT brand, sub_category FROM products "
                "WHERE status='active' AND brand <> '' AND sub_category <> ''"
            ).fetchall()
        ]
        subcats = [
            r["sub_category"]
            for r in conn.execute(
                "SELECT DISTINCT sub_category FROM products "
                "WHERE status='active' AND sub_category <> ''"
            ).fetchall()
        ]

    random.shuffle(pairs)
    random.shuffle(subcats)
    # Mix brand-specific terms with bare subcategories, all derived from inventory.
    hot: list[str] = []
    for brand, sub in pairs[:5]:
        # Use the first token of bilingual brand names for natural suggestions.
        brand_short = brand.split()[0]
        hot.append(f"{brand_short}{sub}")
    hot += subcats[:5]
    random.shuffle(hot)

    return {
        "categories": cats,
        "hot_searches": hot[:8],
    }
