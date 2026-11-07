from __future__ import annotations

"""FastAPI 应用：暴露 Agent 的 HTTP / SSE 接口。

端点：
    POST /chat          非流式，一次性返回 JSON
    POST /chat/stream   SSE 流式，事件序列：meta / tool_result / token / done / error
    GET  /health        健康检查

会话管理：
    极简实现——客户端传 session_id（uuid），服务端用进程内字典存。
    单 worker 部署够用；要横向扩展时换 Redis。
"""

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

# 必须在 import chromadb / sentence_transformers 之前完成环境设置
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
from store.cart_store import CartNotFoundError, CartStore
from store.order_store import OrderStore
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
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    query: str = Field(..., description="用户输入的自然语言 query")
    session_id: str | None = Field(None, description="会话 id；不传则服务端新建")
    user_id: str | None = Field(None, description="用户 id；用于加载跨会话 Preference 核心记忆")
    image_base64: str | None = Field(
        None,
        description="拍照找货：图片的 base64（可带 data:image/...;base64, 前缀，也可纯 base64）",
    )
    image_url: str | None = Field(
        None, description="拍照找货：图片的远程 URL（与 image_base64 二选一）"
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
    user_text: str = Field(..., description="用户首条消息")
    assistant_text: str | None = Field(None, description="AI 首条回复（可空）")


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


class PreferenceUndoRequest(BaseModel):
    undo_token: str = Field(..., description="memory_update 事件返回的撤销 token")


class CheckoutRequest(BaseModel):
    user_id: str | None = Field(None, description="下单的用户；历史订单和再买一次按它查")
    cart_item_ids: list[str] = Field(default_factory=list, description="要下单的购物车行；不传则下单所有勾选项")
    address: str | None = None


class MemoryTellRequest(BaseModel):
    text: str = Field(..., description="用户在记忆页直接说的一句话")


class MemorySettingsRequest(BaseModel):
    personalization_enabled: bool


# ---------------------------------------------------------------------------
# 应用 & 会话管理
# ---------------------------------------------------------------------------

app = FastAPI(title="E-commerce AI Agent", version="0.2.0")

# 商品详情 / 批量查询端点（队友 wsm 贡献）
app.include_router(products_router.router)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """统一错误响应格式：dict detail 透传；字符串 detail 包装成标准 envelope。"""
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
_agent: Any | None = None  # 懒加载，避免 import 时就触发 SearchService/OpenAI 初始化
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


class _SessionTurnGate:
    """同一会话同时只跑一个流式回合。

    用户点停止后马上发新消息时，旧回合可能还在等工具返回。会话是整段读写的，
    不等旧回合保存完就加载，两边会互相覆盖。等待有上限，异常遗留的回合不会卡死新请求。
    只在进程内生效，多 Worker 部署需要换成 Redis 锁。
    """

    def __init__(self, wait_seconds: float = 6.0) -> None:
        self._wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}

    def begin(self, session_id: str | None) -> threading.Event | None:
        if not session_id:
            return None
        with self._lock:
            prior = self._active.get(session_id)
        if prior is not None and not prior.wait(self._wait_seconds):
            logger.warning("session %s: previous turn still running, starting anyway", session_id)
        turn = threading.Event()
        with self._lock:
            self._active[session_id] = turn
        return turn

    def finish(self, session_id: str | None, turn: threading.Event | None) -> None:
        if not session_id or turn is None:
            return
        turn.set()
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
    memory_service.extract_in_background(_user_memory, user_id, query, session_id, events)
    if events:
        session.user_profile = _user_memory.get_preference(user_id).to_dict()
    return events


@app.on_event("startup")
def _warmup() -> None:
    """启动预热：在后台线程把 Agent / SearchService / embedding / API rerank 拉热。

    否则首条真实查询会在 SSE 请求内部触发 ~30-90s 的模型懒加载，
    导致流卡在 status(tool) 之后迟迟收不到 tool_result。

    预热放在后台线程（而非直接在 startup 里同步执行），这样 uvicorn 能
    立即绑定端口、/health 立即可用；模型在后台加热，首条查询若赶在加热
    完成前到达，也只是退化为原来的懒加载行为，不会让整个服务起不来。
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
            # 跑一次真实检索，强制 embedding + API rerank HTTP client 完成初始化
            svc.search("预热查询", top_k_products=1)
            # 拍照找货的视觉索引（百炼主索引）也提前读进内存，否则首次图搜多等 1-2s
            try:
                from search.visual_index import get_visual_index

                get_visual_index("dashscope")
            except Exception as exc:  # noqa: BLE001 - 没建视觉索引时图搜会自行退化为纯文本
                logger.warning("视觉索引预热跳过：%r", exc)
            _warmup_status.update(status="ready", message="Agent/SearchService warmup completed.")
            logger.info("Warmup done: models are hot.")
        except Exception as exc:  # noqa: BLE001 - 预热失败不应阻止服务运行
            _warmup_status.update(status="failed", message=f"Warmup failed: {type(exc).__name__}: {exc}")
            logger.exception("Warmup failed (服务仍可用，首条查询会较慢)")

    threading.Thread(target=_run, name="warmup", daemon=True).start()


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
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
    return {"message": "已忘掉这条", "undo_token": result["undo_token"], "view": memory_service.build_view(_user_memory, clean_user_id)}


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
    return {"message": "已恢复", "view": memory_service.build_view(_user_memory, clean_user_id)}


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
    _sessions.save(session)  # 持久化本轮对话，重启后可接上下文
    return ChatResponse(
        session_id=session.session_id,
        decision=resp.decision.to_dict(),
        tool_result=resp.tool_result.to_dict(),
        narrative=resp.narrative,
        trace=resp.trace,
    )


def _resolve_image(req: ChatRequest) -> str | None:
    """把请求里的图片归一成 OpenAI 兼容 image_url 能直接使用的形态。

    优先用远程 URL；否则用 base64（裸 base64 自动补 data URI 前缀）。
    都没有则返回 None（走普通文本链路）。
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

    输出格式（每条事件以 `\\n\\n` 结束）：
        event: <session|status|meta|tool_result|token|done|error>
        data: <json>

    事件顺序：
        session → status(routing) → meta → status(tool) → tool_result
                → status(compose)? → narrative* | token* → token(完整 JSON，兼容) → done

    `narrative.data.part` ∈ opening(逐字 delta) / opening_end / group(分区标题)
    / item(一款商品的解说，带所属分区) / summary(结尾总结) / followup(一条追问)；
    前端按到达顺序弹出：先开场白、再逐张卡片、最后总结和追问。

    `status.data.message` 是给用户看的"识别中…/检索中…/生成中…"提示，
    前端可显示成 loading 胶囊；`meta`/`tool_result` 是结构化数据。

    客户端用 `EventSource` 或 `requests.get(stream=True)` 消费。
    """
    turn = _turn_gate.begin(req.session_id)
    try:
        session = _sessions.get_or_create(req.session_id)
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
        except Exception as exc:  # noqa: BLE001 - 流式中任何异常都要给前端
            logger.exception("Stream pipeline crashed")
            yield _format_sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            # 先关闭 Agent 流：客户端中断时半截回合在这一步写入 session。
            # 之后再持久化，history + working_memory 才是完整的，重启可恢复。
            try:
                if stream is not None:
                    stream.close()
                _sessions.save(session)
            finally:
                _turn_gate.finish(req.session_id, turn)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        # 关闭 nginx 之类的代理缓冲，否则 token 会被攒在一起
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
    """清空购物车（演示单用户 demo_user）。

    购物车持久化在 SQLite 里且与会话无关。本端点仅在需要手动清空时使用
    （如调试）；App 冷启动不再自动调用——改为启动时加载已有购物车。
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
    """确定性修改购物车并返回最新快照，供前端购物车页同步持久化状态。"""
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
    except CartNotFoundError as exc:
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
    """读取当前购物车快照（演示单用户 demo_user）。

    购物车持久化在 SQLite，App 冷启动调用本端点恢复购物车，避免"启动即空车"
    与后端真实状态不一致（首次加购时回灌历史、算错总价）。返回结构与 cart
    工具 SSE 里的快照一致：{cart: {lines, item_count, total}}，前端可统一解析。
    """
    return _cart_response(CartStore())


@app.post("/compare")
def compare(req: CompareRequest, request: Request) -> dict[str, Any]:
    """商品对比：给定 2 个商品 id，返回结构化对比表 + 购买建议。

    对话路径走 compare 工具（SSE），这个 REST 端点供「对比页」直接调用，
    两者复用同一个 build_comparison，产出结构一致。
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
    ids = ids[:2]  # 固定对比 2 件

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
    """用首轮对话生成一个 4-8 字的对话标题（供历史列表展示）。

    前端首轮 AI 回复完成后异步调用，拿到精炼标题再回写本地历史；调用失败或
    超时不影响主流程（前端退回截句标题）。生成是确定性约束的轻量 LLM 调用。
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
            timeout=float(os.getenv("LLM_TITLE_TIMEOUT", os.getenv("DASHSCOPE_TITLE_TIMEOUT", "8"))),
        )
        title = (resp.choices[0].message.content or "").strip()
        title = title.strip("「」\"'。.，、 \n\t")[:12]
    except Exception as exc:  # noqa: BLE001 - 失败不应 500；返回空让前端兜底
        logger.warning("标题生成失败：%r", exc)
        title = ""

    if not title:
        title = user_text[:12]
    return {"title": title}


@app.get("/suggestions")
def get_suggestions() -> dict[str, Any]:
    """空态首页推荐：分类入口 + 动态热门搜索，全部源自真实商品库。

    热门词由「品牌+子类目」「子类目」从库里随机取，保证每条点了都有商品命中
    （解决手写示例答不上的问题）；每次请求随机轮换，呈现动态感。
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
        # 真实 (品牌, 子类目) 组合 —— 一定对应到具体商品
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
    # 混合：一半「品牌+子类目」（如 Nike 跑步鞋），一半裸子类目（如 降噪真无线耳机），
    # 都来自真实库存，点击必有结果。
    hot: list[str] = []
    for brand, sub in pairs[:5]:
        # 品牌名可能带中英混排（"Apple 苹果"），取首段更自然
        brand_short = brand.split()[0]
        hot.append(f"{brand_short}{sub}")
    hot += subcats[:5]
    random.shuffle(hot)

    return {
        "categories": cats,
        "hot_searches": hot[:8],
    }
