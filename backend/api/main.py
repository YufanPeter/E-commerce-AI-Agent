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
from typing import Any

# 必须在 import chromadb / sentence_transformers 之前完成环境设置
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_IMPL", "none")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent.orchestrator import Agent
from agent.session import AgentSession
from api import products as products_router


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    query: str = Field(..., description="用户输入的自然语言 query")
    session_id: str | None = Field(None, description="会话 id；不传则服务端新建")


class ChatResponse(BaseModel):
    session_id: str
    decision: dict[str, Any]
    tool_result: dict[str, Any]
    narrative: str
    trace: dict[str, Any]


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


class _SessionStore:
    """进程内 session 存储；够 demo 用。"""

    def __init__(self) -> None:
        self._store: dict[str, AgentSession] = {}

    def get_or_create(self, session_id: str | None) -> AgentSession:
        if session_id and session_id in self._store:
            return self._store[session_id]
        sess = AgentSession(session_id=session_id) if session_id else AgentSession()
        self._store[sess.session_id] = sess
        return sess

    def reset(self, session_id: str) -> None:
        self._store.pop(session_id, None)


_sessions = _SessionStore()
_agent: Agent | None = None  # 懒加载，避免 import 时就触发 SearchService 初始化


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        logger.info("Initializing Agent (will load search service & embeddings)")
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
    import threading

    def _run() -> None:
        try:
            logger.info("Warmup: initializing agent and loading models…")
            from search.search_service import get_search_service

            _get_agent()
            svc = get_search_service()
            # 跑一次真实检索，强制 embedding + API rerank HTTP client 完成初始化
            svc.search("预热查询", top_k_products=1)
            logger.info("Warmup done: models are hot.")
        except Exception:  # noqa: BLE001 - 预热失败不应阻止服务运行
            logger.exception("Warmup failed (服务仍可用，首条查询会较慢)")

    threading.Thread(target=_run, name="warmup", daemon=True).start()


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    session = _sessions.get_or_create(req.session_id)
    resp = _get_agent().handle_turn(req.query, session)
    return ChatResponse(
        session_id=session.session_id,
        decision=resp.decision.to_dict(),
        tool_result=resp.tool_result.to_dict(),
        narrative=resp.narrative,
        trace=resp.trace,
    )


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式端点。

    输出格式（每条事件以 `\\n\\n` 结束）：
        event: <session|status|meta|tool_result|token|done|error>
        data: <json>

    事件顺序：
        session → status(routing) → meta → status(tool) → tool_result
                → status(compose)? → token* → done

    `status.data.message` 是给用户看的"识别中…/检索中…/生成中…"提示，
    前端可显示成 loading 胶囊；`meta`/`tool_result` 是结构化数据。

    客户端用 `EventSource` 或 `requests.get(stream=True)` 消费。
    """
    session = _sessions.get_or_create(req.session_id)
    agent = _get_agent()

    def _format_sse(event: str, payload: Any) -> str:
        data = json.dumps(payload, ensure_ascii=False)
        return f"event: {event}\ndata: {data}\n\n"

    def _generator():
        # 首条：先把 session_id 告诉客户端（便于客户端持久化）
        yield _format_sse("session", {"session_id": session.session_id})
        try:
            for ev in agent.handle_turn_stream(req.query, session):
                yield _format_sse(ev["type"], ev["data"])
        except Exception as exc:  # noqa: BLE001 - 流式中任何异常都要给前端
            logger.exception("Stream pipeline crashed")
            yield _format_sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    headers = {
        "Cache-Control": "no-cache, no-transform",
        # 关闭 nginx 之类的代理缓冲，否则 token 会被攒在一起
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(_generator(), media_type="text/event-stream", headers=headers)


@app.post("/sessions/{session_id}/reset")
def reset_session(session_id: str) -> dict[str, str]:
    _sessions.reset(session_id)
    return {"status": "reset", "session_id": session_id}
