from __future__ import annotations

"""回答反馈：用户对导购回答的点赞点踩。

存下当时的问题和回答，差评可以直接回流成评测样本。同一条回答改主意时覆盖旧评价。
"""

import sqlite3
from pathlib import Path

from store.product_store import DEFAULT_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_feedback (
    message_id TEXT PRIMARY KEY,
    rating     TEXT NOT NULL,
    session_id TEXT,
    user_id    TEXT,
    query      TEXT NOT NULL DEFAULT '',
    answer     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

MAX_STORED_ANSWER_CHARS = 8000


class FeedbackStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(
        self,
        message_id: str,
        rating: str,
        session_id: str | None = None,
        user_id: str | None = None,
        query: str = "",
        answer: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO answer_feedback (message_id, rating, session_id, user_id, query, answer)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    rating = excluded.rating,
                    created_at = datetime('now')
                """,
                (message_id, rating, session_id, user_id, query, answer[:MAX_STORED_ANSWER_CHARS]),
            )

    def get(self, message_id: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM answer_feedback WHERE message_id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None
