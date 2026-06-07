from __future__ import annotations

"""会话持久化：把 AgentSession 存到 SQLite，让后端重启后还能接上多轮上下文。

为什么要它：
    原先 api.main._SessionStore 是进程内裸字典，后端一重启，所有会话的
    history（多轮原文）+ working_memory（last_hits / pending_cart 等）全丢，
    前端历史虽在本地 JSON，但重开后接不上上下文。

存什么：
    AgentSession 是个干净的 dataclass——session_id + history(list[Message]) +
    working_memory(dict)。两者都能无损 JSON 序列化，所以这里就把它们整体存进
    一行（history_json / memory_json），读时反序列化回 AgentSession。

放哪：
    复用商品库同一个 SQLite 文件（DEFAULT_DB_PATH），懒建表 agent_sessions，
    无需改 init.sql / 重灌库。单 worker demo 足够，要横向扩展再换 Redis。
"""

import json
import logging
import sqlite3
from pathlib import Path

from agent.session import AgentSession, Message
from store.product_store import DEFAULT_DB_PATH


logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id   TEXT PRIMARY KEY,
    history_json TEXT NOT NULL,
    memory_json  TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


class SqliteSessionStore:
    """SQLite 持久化的会话存储。接口与原进程内 _SessionStore 兼容。"""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(_CREATE_TABLE_SQL)
        except Exception:  # noqa: BLE001 - 建表失败不应阻断启动；退化为不持久化
            logger.exception("agent_sessions 建表失败，会话将不持久化")

    # ------------------------------ 读 ------------------------------

    def get_or_create(self, session_id: str | None) -> AgentSession:
        """有则从库里恢复，无则新建并落盘。与原字典版接口一致。"""
        if session_id:
            loaded = self._load(session_id)
            if loaded is not None:
                return loaded
        session = AgentSession(session_id=session_id) if session_id else AgentSession()
        self.save(session)
        return session

    def _load(self, session_id: str) -> AgentSession | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT history_json, memory_json FROM agent_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except Exception:  # noqa: BLE001
            logger.exception("读取会话 %s 失败", session_id)
            return None
        if row is None:
            return None
        try:
            history = [
                Message(role=m["role"], content=m["content"])
                for m in json.loads(row["history_json"])
            ]
            memory = json.loads(row["memory_json"])
        except Exception:  # noqa: BLE001 - 脏数据不应让会话崩，丢弃当新会话
            logger.exception("反序列化会话 %s 失败", session_id)
            return None
        return AgentSession(
            session_id=session_id, history=history, working_memory=memory
        )

    # ------------------------------ 写 ------------------------------

    def save(self, session: AgentSession) -> None:
        """整体落盘（覆盖写）。每个 turn 结束后调用，保证重启可恢复。"""
        history_json = json.dumps(
            [{"role": m.role, "content": m.content} for m in session.history],
            ensure_ascii=False,
        )
        try:
            memory_json = json.dumps(session.working_memory, ensure_ascii=False)
        except (TypeError, ValueError):
            # working_memory 理论上都是可序列化的（dict/list/str/num/bool），
            # 万一塞了非常规对象，降级存空，避免写库异常打断请求。
            logger.warning("会话 %s 的 working_memory 不可序列化，降级存空", session.session_id)
            memory_json = "{}"
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_sessions (session_id, history_json, memory_json, updated_at)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(session_id) DO UPDATE SET
                        history_json = excluded.history_json,
                        memory_json  = excluded.memory_json,
                        updated_at   = excluded.updated_at
                    """,
                    (session.session_id, history_json, memory_json),
                )
        except Exception:  # noqa: BLE001 - 持久化失败不应让本轮请求失败
            logger.exception("保存会话 %s 失败", session.session_id)

    def reset(self, session_id: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM agent_sessions WHERE session_id = ?", (session_id,)
                )
        except Exception:  # noqa: BLE001
            logger.exception("删除会话 %s 失败", session_id)
