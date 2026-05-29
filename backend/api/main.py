from __future__ import annotations

"""FastAPI 应用：暴露 Agent 的 HTTP / SSE 接口。

端点：
    POST /chat          非流式，一次性返回 JSON
    POST /chat/stream   SSE 流式，事件序列：meta / tool_result / token / done / error
    GET  /healthz       健康检查

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

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent.orchestrator import Agent
from agent.session import AgentSession


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


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
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
