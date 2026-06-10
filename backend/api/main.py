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
import time
from collections.abc import Iterator
from typing import Any

# 必须在 import chromadb / sentence_transformers 之前完成环境设置
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_IMPL", "none")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api import products as products_router
from store.cart_store import CartStore
from store.session_store import SqliteSessionStore


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    query: str = Field(..., description="用户输入的自然语言 query")
    session_id: str | None = Field(None, description="会话 id；不传则服务端新建")
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


class CompareRequest(BaseModel):
    product_ids: list[str] = Field(..., description="要对比的商品 id（2-3 个）")
    focus: str | None = Field(None, description="对比侧重点，如『续航』『适合敏感肌』")


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


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    session = _sessions.get_or_create(req.session_id)
    resp = _get_agent().handle_turn(req.query, session)
    _sessions.save(session)  # 持久化本轮对话，重启后可接上下文
    return ChatResponse(
        session_id=session.session_id,
        decision=resp.decision.to_dict(),
        tool_result=resp.tool_result.to_dict(),
        narrative=resp.narrative,
        trace=resp.trace,
    )


def _resolve_image(req: ChatRequest) -> str | None:
    """把请求里的图片归一成 Ark image_url 字段能直接吃的形态。

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


def _format_sse(event: str, payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def _chat_event_generator(
    req: ChatRequest,
    session: Any,
    request_started_at: float,
) -> Iterator[str]:
    # 首包不依赖 Agent/SearchService：让客户端立刻拿到 session 和可展示的状态。
    yield _format_sse("session", {"session_id": session.session_id})
    yield _format_sse(
        "status",
        {
            "phase": "startup",
            "message": "正在连接导购引擎…",
            "elapsed_ms": int((time.perf_counter() - request_started_at) * 1000),
        },
    )

    try:
        agent = _get_agent()
        image = _resolve_image(req)
        # 有图 → 走图搜（拍照找货）；query 作为随图附带文字（可空）
        if image:
            stream = agent.handle_image_turn_stream(
                image, session, hint_text=req.query
            )
        else:
            stream = agent.handle_turn_stream(req.query, session)
        for ev in stream:
            yield _format_sse(ev["type"], ev["data"])
    except Exception as exc:  # noqa: BLE001 - 流式中任何异常都要给前端
        logger.exception("Stream pipeline crashed")
        yield _format_sse("error", {"message": f"{type(exc).__name__}: {exc}"})
    finally:
        # 流结束（含正常/异常）后持久化会话，history + working_memory 都已在
        # 生成器内部更新完毕，此时落盘保证重启可恢复。
        _sessions.save(session)


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式端点。

    输出格式（每条事件以 `\\n\\n` 结束）：
        event: <session|status|meta|tool_result|token|done|error>
        data: <json>

    事件顺序：
        session → status(startup) → status(routing) → meta → status(tool)
                → tool_result → status(compose)? → token* → done

    `status.data.message` 是给用户看的"识别中…/检索中…/生成中…"提示，
    前端可显示成 loading 胶囊；`meta`/`tool_result` 是结构化数据。

    客户端用 `EventSource` 或 `requests.get(stream=True)` 消费。
    """
    request_started_at = time.perf_counter()
    session = _sessions.get_or_create(req.session_id)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        # 关闭 nginx 之类的代理缓冲，否则 token 会被攒在一起
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _chat_event_generator(req, session, request_started_at),
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


@app.get("/cart")
def get_cart() -> dict[str, Any]:
    """读取当前购物车快照（演示单用户 demo_user）。

    购物车持久化在 SQLite，App 冷启动调用本端点恢复购物车，避免"启动即空车"
    与后端真实状态不一致（首次加购时回灌历史、算错总价）。返回结构与 cart
    工具 SSE 里的快照一致：{cart: {lines, item_count, total}}，前端可统一解析。
    """
    return _cart_response(CartStore())


@app.post("/compare")
def compare(req: CompareRequest) -> dict[str, Any]:
    """商品对比：给定 2 个商品 id，返回结构化对比表 + 购买建议。

    对话路径走 compare 工具（SSE），这个 REST 端点供「对比页」直接调用，
    两者复用同一个 build_comparison，产出结构一致。
    """
    from agent.comparison import build_comparison
    from store.product_store import ProductStore

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

    return build_comparison(details, focus=(req.focus or ""))


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
            timeout=float(os.getenv("ARK_TITLE_TIMEOUT", "8")),
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
