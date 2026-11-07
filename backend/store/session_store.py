from __future__ import annotations

"""Persist ``AgentSession`` in SQLite so multi-turn context survives restarts.

The original in-memory store lost history and working memory whenever the backend
restarted, even though the client retained its own local display history.

``AgentSession`` history and working memory are JSON-serializable and stored together in
one row, then reconstructed on load.

The store reuses the catalog database and lazily creates ``agent_sessions``. This suits a
single-worker demo; horizontal scaling can move session state to a shared service.
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
    """SQLite session store compatible with the original in-memory API."""

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
        except Exception:  # noqa: BLE001 - table creation must not block startup
            logger.exception("Failed to create agent_sessions; sessions will not be persisted")

    # ------------------------------ Read ------------------------------

    def get_or_create(self, session_id: str | None) -> AgentSession:
        """Restore a stored session or create and persist a new one."""
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
            logger.exception("Failed to read session %s", session_id)
            return None
        if row is None:
            return None
        try:
            history = [
                Message(role=m["role"], content=m["content"])
                for m in json.loads(row["history_json"])
            ]
            memory = json.loads(row["memory_json"])
        except Exception:  # noqa: BLE001 - corrupt state becomes a new session
            logger.exception("Failed to deserialize session %s", session_id)
            return None
        return AgentSession(
            session_id=session_id, history=history, working_memory=memory
        )

    # ------------------------------ Write ------------------------------

    def save(self, session: AgentSession) -> None:
        """Overwrite persisted state after a turn so restarts can recover it."""
        history_json = json.dumps(
            [{"role": m.role, "content": m.content} for m in session.history],
            ensure_ascii=False,
        )
        try:
            memory_json = json.dumps(session.working_memory, ensure_ascii=False)
        except (TypeError, ValueError):
            # Working memory should be JSON-safe; fall back to an empty object if not.
            logger.warning("Session %s has non-serializable working memory; storing an empty value", session.session_id)
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
        except Exception:  # noqa: BLE001 - persistence must not fail the turn
            logger.exception("Failed to save session %s", session.session_id)

    def reset(self, session_id: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM agent_sessions WHERE session_id = ?", (session_id,)
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to delete session %s", session_id)
