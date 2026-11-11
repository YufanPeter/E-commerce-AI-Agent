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
import re
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

# Configure the environment before importing Chroma or sentence-transformers.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_IMPL", "none")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import iterate_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent import memory_service
from api import products as products_router
from catalog_config import PROFILE
from i18n import en as english
from i18n import is_english, t
from store.cart_store import CartNotFoundError, CartStockError, CartStore
from store.order_store import OrderStore
from store.feedback_store import FeedbackStore
from store.session_store import SqliteSessionStore
from store.user_memory_store import UserMemoryStore, UserPreference


logger = logging.getLogger(__name__)

# uvicorn 只给自己的 logger 配 handler，应用层 INFO（各阶段耗时、缓存命中、fallback）默认看不到。
# 这里给 root 配一个最简 handler；LOG_LEVEL=WARNING 可以关掉。
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


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


class CompareItem(BaseModel):
    product_id: str
    sku_id: str | None = Field(None, description="指定对比的规格；不传则按预算 / 最便宜自动选")


class CompareRequest(BaseModel):
    product_ids: list[str] = Field(default_factory=list, description="要对比的商品 id（2 个）；也可用 items 带规格")
    items: list[CompareItem] = Field(default_factory=list, description="商品 + 可选规格，优先于 product_ids")
    focus: str | None = Field(None, description="对比侧重点，如『续航』『适合敏感肌』")
    budget_max: float | None = Field(None, description="预算上限：自动选规格时取预算内最便宜的")
    budget_min: float | None = None


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
    avoid_terms: list[str] | None = None  # 不传表示保持不变：只有确认过的忌讳才会进来


class AnswerFeedbackRequest(BaseModel):
    message_id: str = Field(..., description="这条回答在客户端的 ID，同一条重复提交时覆盖")
    rating: str = Field(..., description="up / down")
    session_id: str | None = None
    user_id: str | None = None
    query: str = Field("", description="这条回答对应的用户问题")
    answer: str = Field("", description="这条回答的文字内容")


class PreferenceUndoRequest(BaseModel):
    undo_token: str = Field(..., description="Undo token returned by a memory_update event")


class CheckoutRequest(BaseModel):
    user_id: str | None = Field(None, description="下单的用户；历史订单和再买一次按它查")
    cart_item_ids: list[str] = Field(default_factory=list, description="要下单的购物车行；不传则下单所有勾选项")
    address: str | None = None


class MemoryTellRequest(BaseModel):
    text: str = Field(..., description="用户在记忆页直接说的一句话")


class MemorySettingsRequest(BaseModel):
    personalization_enabled: bool


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
    if is_english() and re.search(r"[\u4e00-\u9fff]", str(detail.get("message", ""))):
        messages = {
            400: "Check the request and try again.",
            404: "The requested item was not found.",
            409: "This action cannot be completed for the current item.",
        }
        detail = {**detail, "message": messages.get(exc.status_code, "The request could not be completed.")}
    return JSONResponse(status_code=exc.status_code, content=detail)


if PROFILE.requires_prebuilt_db and not PROFILE.db_path.is_file():
    raise RuntimeError("Amazon catalog not found. Run bash scripts/start_amazon.sh to build it first.")

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


class _StreamTurn:
    """一个正在跑的流式回合。"""

    def __init__(self, query: str, replay: list[str]) -> None:
        self.query = query
        # 卡住被放弃的旧回合的问题。它们没来得及保存，新回合要补进历史才能接上需求。
        self.replay = replay
        self.finished = threading.Event()
        self.abandoned = False
        self.saving = False


class _SessionTurnGate:
    """同一会话同时只跑一个流式回合。

    用户边跑边改时，旧回合可能还卡在模型或工具调用里。会话是整段读写的，
    不等旧回合保存完就加载，两边会互相覆盖。但也不能让用户一直等：
    超过等待时间就放弃旧回合，它不再保存，它的问题交给新回合补进历史。
    只在进程内生效，多 Worker 部署需要换成 Redis 锁。
    """

    def __init__(self, wait_seconds: float = 3.0, save_grace_seconds: float = 2.0) -> None:
        self._wait_seconds = wait_seconds
        self._save_grace_seconds = save_grace_seconds
        self._lock = threading.Lock()
        self._active: dict[str, _StreamTurn] = {}

    def begin(self, session_id: str | None, query: str = "") -> _StreamTurn | None:
        if not session_id:
            return None
        with self._lock:
            prior = self._active.get(session_id)
        replay: list[str] = []
        if prior is not None and not prior.finished.wait(self._wait_seconds):
            with self._lock:
                is_saving = prior.saving
                if not is_saving:
                    prior.abandoned = True
                    replay = [*prior.replay, prior.query]
            if is_saving:
                prior.finished.wait(self._save_grace_seconds)
            else:
                logger.warning("session %s: abandoned a stuck turn for the new one", session_id)
        turn = _StreamTurn(query, replay)
        with self._lock:
            self._active[session_id] = turn
        return turn

    def claim_save(self, turn: _StreamTurn | None) -> bool:
        """旧回合保存前调用。已被放弃的回合不能再写会话，否则会盖掉新回合。"""
        if turn is None:
            return True
        with self._lock:
            if turn.abandoned:
                return False
            turn.saving = True
            return True

    def finish(self, session_id: str | None, turn: _StreamTurn | None) -> None:
        if not session_id or turn is None:
            return
        turn.finished.set()
        with self._lock:
            if self._active.get(session_id) is turn:
                del self._active[session_id]


_turn_gate = _SessionTurnGate()


async def _close_when_finished(generator: Iterator[str]) -> AsyncIterator[str]:
    """在线程池里迭代 SSE 生成器，并保证断连后立刻关闭它。

    不显式关闭的话，收尾要等垃圾回收，上游 LLM 流和会话保存的时机都不确定。
    取消信号要等当前这次 next() 返回才生效，所以关闭时生成器不会正在执行。
    """
    try:
        async for chunk in iterate_in_threadpool(generator):
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(generator.close)


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
    inferred = memory_service.inferred_signals(_user_memory, clean_user_id)
    if inferred:
        session.user_profile["_inferred"] = inferred


def _apply_realtime_preference_updates(query: str, session: Any) -> list[dict[str, Any]]:
    user_id = getattr(session, "user_id", None)
    if not user_id:
        return []
    session_id = getattr(session, "session_id", None)
    events = memory_service.process_utterance(_user_memory, user_id, query, session_id)
    if PROFILE.capabilities.llm_memory:
        memory_service.extract_in_background(_user_memory, user_id, query, session_id, events)
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
            # 拍照找货的视觉索引（百炼主索引）也提前读进内存，否则首次图搜多等 1-2s
            try:
                from search.visual_index import get_visual_index

                get_visual_index("dashscope")
            except Exception as exc:  # noqa: BLE001 - 没建视觉索引时图搜会自行退化为纯文本
                logger.warning("视觉索引预热跳过：%r", exc)
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
    if PROFILE.requires_prebuilt_db:
        try:
            from contextlib import closing

            with closing(sqlite3.connect(f"{PROFILE.db_path.as_uri()}?mode=ro", uri=True)) as conn:
                row = conn.execute(
                    "SELECT 1 FROM amazon_product_metadata a JOIN products p USING (product_id) "
                    "WHERE p.base_price = a.price_usd LIMIT 1"
                ).fetchone()
            if row is None:
                raise ValueError("Amazon USD catalog is empty")
        except (sqlite3.Error, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Build the Amazon USD catalog before starting the backend.") from exc
    return {"status": "ok"}


@app.get("/warmup")
def warmup_status() -> dict[str, str]:
    return dict(_warmup_status)


@app.get("/llm/status")
def llm_status() -> dict[str, Any]:
    """当前 Chat provider 链与熔断状态（排查"首字慢 / 走了 fallback"用）。"""
    from llm.client import get_provider_name, provider_status

    try:
        primary = get_provider_name()
    except Exception as exc:  # noqa: BLE001
        return {"primary": None, "error": str(exc), "backends": []}
    return {"primary": primary, "backends": provider_status()}


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
    if data.get("avoid_terms") is None:
        data["avoid_terms"] = _user_memory.get_preference(clean_user_id).avoid_terms
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


def _memory_user(user_id: str) -> str:
    clean_user_id = user_id.strip()
    if not clean_user_id:
        raise HTTPException(status_code=400, detail="user_id 不能为空")
    return clean_user_id


@app.get("/memory/{user_id}")
def get_memory(user_id: str) -> dict[str, Any]:
    """记忆页：生效的记忆（带来源和影响）、待确认建议、最近活动。"""
    return memory_service.build_view(_user_memory, _memory_user(user_id))


@app.post("/memory/{user_id}/tell")
def tell_memory(user_id: str, req: MemoryTellRequest) -> dict[str, Any]:
    """用户直接说一句话，能听出结构的进结构化记忆，其余记成备注。"""
    clean_user_id = _memory_user(user_id)
    result = memory_service.tell(_user_memory, clean_user_id, req.text)
    return {**result, "view": memory_service.build_view(_user_memory, clean_user_id)}


@app.post("/memory/{user_id}/items/{item_id}/confirm")
def confirm_memory(user_id: str, item_id: str) -> dict[str, Any]:
    clean_user_id = _memory_user(user_id)
    event = memory_service.confirm_suggestion(_user_memory, clean_user_id, item_id)
    if event is None:
        raise HTTPException(status_code=404, detail="这条建议不存在")
    return {"message": event["message"], "undo_token": event.get("undo_token"), "view": memory_service.build_view(_user_memory, clean_user_id)}


@app.post("/memory/{user_id}/items/{item_id}/reject")
def reject_memory(user_id: str, item_id: str) -> dict[str, Any]:
    clean_user_id = _memory_user(user_id)
    if not memory_service.reject_suggestion(_user_memory, clean_user_id, item_id):
        raise HTTPException(status_code=404, detail="这条建议不存在")
    return {"message": "好的，不再提这条", "view": memory_service.build_view(_user_memory, clean_user_id)}


@app.delete("/memory/{user_id}/items/{item_id}")
def delete_memory_item(user_id: str, item_id: str) -> dict[str, Any]:
    clean_user_id = _memory_user(user_id)
    result = _user_memory.remove_item(clean_user_id, item_id)
    if result is None:
        raise HTTPException(status_code=404, detail="这条记忆不存在")
    return {"message": t("memory.removed"), "undo_token": result["undo_token"], "view": memory_service.build_view(_user_memory, clean_user_id)}


@app.put("/memory/{user_id}/settings")
def update_memory_settings(user_id: str, req: MemorySettingsRequest) -> dict[str, Any]:
    clean_user_id = _memory_user(user_id)
    preference = _user_memory.get_preference(clean_user_id)
    preference.personalization_enabled = req.personalization_enabled
    _user_memory.put_preference(preference)
    return memory_service.build_view(_user_memory, clean_user_id)


@app.post("/memory/{user_id}/undo")
def undo_memory(user_id: str, req: PreferenceUndoRequest) -> dict[str, Any]:
    clean_user_id = _memory_user(user_id)
    result = _user_memory.undo_update(clean_user_id, req.undo_token.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="撤销 token 不存在或已使用")
    return {"message": t("memory.restored"), "view": memory_service.build_view(_user_memory, clean_user_id)}


@app.delete("/memory/{user_id}")
def clear_memory(user_id: str) -> dict[str, Any]:
    """彻底忘掉这个用户：偏好、记忆、行为记录全部删除，不可恢复。"""
    clean_user_id = _memory_user(user_id)
    _user_memory.clear_user(clean_user_id)
    return memory_service.build_view(_user_memory, clean_user_id)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    session = _sessions.get_or_create(req.session_id)
    _attach_user_memory(session, req.user_id)
    resp = _get_agent().handle_turn(req.query, session)
    memory_service.record_turn(_user_memory, session.user_id, session.session_id, resp.tool_result.to_dict(), req.query)
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
    """把请求里的图片归一成 OpenAI 兼容 image_url 能直接使用的形态。

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
def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """SSE 流式端点。

    Each event ends with ``\\n\\n``:
        event: <session|status|meta|tool_result|token|done|error>
        data: <json>

    Event order:
        session → status(routing) → meta → status(tool) → tool_result
                → status(compose)? → narrative* | token* → token(完整 JSON，兼容) → done

    `narrative.data.part` ∈ opening(逐字 delta) / opening_end / group(分区标题)
    / item(一款商品的解说，带所属分区) / summary(结尾总结) / followup(一条追问)；
    前端按到达顺序弹出：先开场白、再逐张卡片、最后总结和追问。

    ``status.data.message`` is user-facing progress copy; ``meta`` and ``tool_result``
    contain structured data.

    Clients may consume the stream with ``EventSource`` or a streaming HTTP client.
    """
    if not PROFILE.capabilities.vision and _resolve_image(req):
        raise HTTPException(status_code=422, detail="Image matching is not configured for the Amazon catalog. Describe the product in English instead.")
    turn = _turn_gate.begin(req.session_id, req.query or "（发了一张图片）")
    try:
        session = _sessions.get_or_create(req.session_id)
        if turn is not None and turn.replay:
            from agent.orchestrator import record_abandoned_turn

            for abandoned_query in turn.replay:
                record_abandoned_turn(session, abandoned_query)
        _attach_user_memory(session, req.user_id)
        agent = _get_agent()
        image = _resolve_image(req)
        image_base = products_router.image_base_url(request)
    except BaseException:
        _turn_gate.finish(req.session_id, turn)
        raise

    def _format_sse(event: str, payload: Any) -> str:
        data = json.dumps(payload, ensure_ascii=False)
        return f"event: {event}\ndata: {data}\n\n"

    def _generator():
        # 首条：先把 session_id 告诉客户端（便于客户端持久化）
        stream = None
        try:
            yield _format_sse("session", {"session_id": session.session_id})
            # 有图 → 走图搜（拍照找货）；query 作为随图附带文字（可空）
            if image:
                stream = agent.handle_image_turn_stream(
                    image, session, hint_text=req.query
                )
            else:
                stream = agent.handle_turn_stream(req.query, session)
            for ev in stream:
                if turn is not None and turn.abandoned:
                    break
                # done 之前抽取本轮偏好更新并推送 memory_update（前端横幅依赖）
                if ev["type"] == "done":
                    for memory_event in _apply_realtime_preference_updates(req.query, session):
                        yield _format_sse("memory_update", memory_event)
                if ev["type"] == "tool_result":
                    memory_service.record_turn(
                        _user_memory, session.user_id, session.session_id, ev["data"] or {}, req.query
                    )
                    # 对比表等 Agent 层产物里的商品图改成后端自己的 /images/{id}
                    products_router.absolutize_image_urls(
                        (ev["data"] or {}).get("payload"), image_base
                    )
                yield _format_sse(ev["type"], ev["data"])
        except Exception as exc:  # noqa: BLE001 - every stream failure reaches the client
            logger.exception("Stream pipeline crashed")
            yield _format_sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            # 先关闭 Agent 流：客户端中断时半截回合在这一步写入 session。
            # 之后再持久化，history + working_memory 才是完整的，重启可恢复。
            try:
                if stream is not None:
                    stream.close()
                if _turn_gate.claim_save(turn):
                    _sessions.save(session)
            finally:
                _turn_gate.finish(req.session_id, turn)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        # Disable proxy buffering so tokens are delivered immediately.
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _close_when_finished(_generator()),
        media_type="text/event-stream",
        headers=headers,
    )


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
    if not PROFILE.capabilities.cart and action in {"add", "updateSpecification"}:
        raise HTTPException(status_code=409, detail=products_router.error_payload(
            "PRODUCT_NOT_PURCHASABLE", t("cart.unavailable"), retryable=False,
        ))
    try:
        _apply_cart_mutation(store, req)
    except CartStockError as exc:
        raise HTTPException(status_code=409, detail=products_router.error_payload(
            "OUT_OF_STOCK", str(exc), retryable=False,
        )) from exc
    return _cart_response(store)


def _apply_cart_mutation(store: CartStore, req: CartMutationRequest) -> None:
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
        if req.skuID == current.sku_id:
            store.set_quantity(old_id, quantity)
        else:
            # Add first: a rejected replacement must leave the existing line intact.
            store.add_product(req.productID, sku_id=req.skuID, quantity=quantity)
            store.remove_item(old_id)
    else:
        raise HTTPException(status_code=400, detail="不支持的购物车操作")


@app.post("/cart/checkout")
def checkout_cart(req: CheckoutRequest) -> dict[str, Any]:
    """购物车页下单：写入订单并移出已下单的行，返回订单和最新购物车。"""
    from store.cart_store import DEFAULT_ADDRESS

    store = CartStore()
    user_id = (req.user_id or "").strip() or None
    try:
        order = store.build_order(
            address=(req.address or DEFAULT_ADDRESS),
            order_user_id=user_id,
            cart_item_ids=[_cart_item_id(value) for value in req.cart_item_ids] or None,
        )
    except (CartNotFoundError, CartStockError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    order_payload = order.to_dict()
    memory_service.record_turn(
        _user_memory, user_id, None,
        {"tool_name": "cart", "payload": {"action": "checkout", "order": order_payload}},
        "购物车结算",
    )
    return {"order": order_payload, **_cart_response(store)}


@app.get("/orders/{user_id}")
def list_orders(user_id: str) -> dict[str, Any]:
    """历史订单，最近的在前。"""
    return {"orders": OrderStore().list_orders(_memory_user(user_id))}


@app.get("/cart")
def get_cart() -> dict[str, Any]:
    """Return the current cart snapshot for the demonstration user.

    Startup restoration keeps client state consistent with SQLite. The response matches
    the cart snapshot emitted by SSE so clients can share one parser.
    """
    return _cart_response(CartStore())


@app.post("/compare")
def compare(req: CompareRequest, request: Request) -> dict[str, Any]:
    """商品对比：给定 2 个商品 id，返回结构化对比表 + 购买建议。

    The conversational tool and comparison screen share ``build_comparison``.
    """
    from agent.comparison import build_comparison
    from store.product_store import ProductStore

    selections: dict[str, str] = {}
    if req.items:
        ids = [it.product_id.strip() for it in req.items if it.product_id and it.product_id.strip()]
        selections = {it.product_id.strip(): it.sku_id for it in req.items if it.sku_id}
    else:
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

    comparison = build_comparison(
        details,
        focus=(req.focus or ""),
        selections=selections or None,
        budget_max=req.budget_max,
        budget_min=req.budget_min,
    )
    products_router.absolutize_image_urls(
        {"comparison": comparison}, products_router.image_base_url(request)
    )
    return comparison


@app.post("/title")
def generate_title(req: TitleRequest) -> dict[str, str]:
    """Generate a short title from the first conversation turn.

    The client calls this asynchronously and falls back to a local truncated title on
    failure or timeout.
    """
    user_text = (req.user_text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text 不能为空")
    words_title = " ".join(user_text.split()[:7])[:70]
    if is_english() and not PROFILE.capabilities.llm_copy:
        return {"title": words_title}

    from llm.client import get_client, get_model_id

    system = english.TITLE_SYSTEM if is_english() else (
        "你是对话标题生成器。根据用户的购物需求，生成一个简洁的中文标题，"
        "用于历史记录列表展示。要求：① 4-8 个字；② 概括核心需求（品类+关键属性），"
        "如「油皮洗面奶」「平价蓝牙耳机」「三亚出行搭配」；③ 只输出标题本身，"
        "不要引号、标点、解释。"
    )
    convo = f"{'Shopper' if is_english() else '用户'}：{user_text}"
    if req.assistant_text and req.assistant_text.strip():
        convo += f"\n{'Assistant' if is_english() else '助手'}：{req.assistant_text.strip()[:200]}"

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
            timeout=float(os.getenv("LLM_TITLE_TIMEOUT", os.getenv("DASHSCOPE_TITLE_TIMEOUT", "8"))),
        )
        title = (resp.choices[0].message.content or "").strip()
        title = title.strip("「」“”\"'。.，、 \n\t")[:60 if is_english() else 12]
    except Exception as exc:  # noqa: BLE001 - return empty so the client can fall back
        logger.warning("Title generation failed: %r", exc)
        title = ""

    if not title:
        title = words_title if is_english() else user_text[:12]
    return {"title": title}


_feedback = FeedbackStore()


@app.post("/feedback")
def submit_feedback(req: AnswerFeedbackRequest) -> dict[str, str]:
    """回答下方的点赞点踩。存下问题和回答，之后可以回流成评测样本。"""
    if req.rating not in {"up", "down"}:
        raise HTTPException(status_code=400, detail="rating 只能是 up 或 down")
    if not req.message_id.strip():
        raise HTTPException(status_code=400, detail="message_id 不能为空")
    _feedback.save(
        message_id=req.message_id.strip(),
        rating=req.rating,
        session_id=req.session_id,
        user_id=req.user_id,
        query=req.query,
        answer=req.answer,
    )
    return {"status": "ok"}


@app.get("/home/{user_id}")
def get_home(user_id: str, request: Request) -> dict[str, Any]:
    """空态首页的个性化入口：用户最近在看的品类，每条配一张真实商品图。"""
    from store.product_store import ProductStore

    clean_user_id = _memory_user(user_id)
    recent = memory_service.recent_interests(_user_memory, clean_user_id)
    store = ProductStore()
    image_base = products_router.image_base_url(request)
    entries = []
    for interest in recent["interests"]:
        detail = _home_entry_product(store, interest)
        if detail is None:
            continue  # 品类已下架：点了也搜不到，不展示
        entries.append({
            "title": t("home.keep_browsing", label=interest["label"]),
            "query": t("home.browse_query", label=interest["label"]),
            "image_url": products_router.resolve_image_url(detail, image_base),
        })
    return {"returning": recent["returning"], "entries": entries}


def _home_entry_product(store: Any, interest: dict[str, Any]) -> Any:
    if interest.get("product_id"):
        detail = store.get_product_detail(interest["product_id"])
        if detail is not None:
            return detail
    column, value = (
        ("sub_category", interest["sub_category"])
        if interest.get("sub_category")
        else ("category", interest.get("category"))
    )
    if not value:
        return None
    with store.connect() as conn:
        row = conn.execute(
            f"SELECT product_id FROM products WHERE status='active' AND {column} = ? LIMIT 1",
            (value,),
        ).fetchone()
    return store.get_product_detail(row["product_id"]) if row else None


def _hot_search_term(brand: str, sub_category: str) -> str:
    """拼热搜词。中英混排的品牌（"Apple 苹果"）只取一段；纯英文品牌（"The Ordinary"）不能截断。"""
    parts = brand.split()
    has_chinese = any("\u4e00" <= char <= "\u9fff" for char in brand)
    if len(parts) > 1 and has_chinese:
        return f"{parts[0]}{sub_category}"
    return f"{brand} {sub_category}" if parts[-1:] and parts[-1].isascii() else f"{brand}{sub_category}"


# Amazon has thousands of small or generic brands, so only well-reviewed pairs are suggested.
_HOT_PAIRS_SQL = {
    False: (
        "SELECT DISTINCT brand, sub_category FROM products "
        "WHERE status='active' AND brand <> '' AND sub_category <> ''"
    ),
    True: (
        "SELECT p.brand, p.sub_category FROM products p "
        "JOIN amazon_product_metadata a ON a.product_id = p.product_id "
        "WHERE p.status='active' AND p.brand <> '' AND a.is_generic_brand = 0 "
        "GROUP BY p.brand, p.sub_category HAVING SUM(a.rating_number) >= 5000"
    ),
}


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
            for r in conn.execute(_HOT_PAIRS_SQL[PROFILE.search_backend == "catalog"]).fetchall()
            if r["brand"].isascii()
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
        hot.append(_hot_search_term(brand, sub))
    hot += subcats[:5]
    random.shuffle(hot)

    return {
        "categories": cats,
        "hot_searches": hot[:8],
    }
