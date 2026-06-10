from __future__ import annotations

"""User preference persistence with lightweight undo support."""

import json
import logging
import secrets
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from store.product_store import DEFAULT_DB_PATH


logger = logging.getLogger(__name__)


@dataclass
class UserPreference:
    """Cross-session, user-editable core memory."""

    user_id: str
    personalization_enabled: bool = True
    budget_min: float | None = None
    budget_max: float | None = None
    price_tier: str | None = None
    favorite_categories: list[str] = field(default_factory=list)
    brand_include: list[str] = field(default_factory=list)
    brand_exclude: list[str] = field(default_factory=list)
    preference_keywords: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    category_specific: dict[str, dict[str, Any]] = field(default_factory=dict)
    preference_note: str | None = None
    notes: str | None = None

    @classmethod
    def empty(cls, user_id: str) -> "UserPreference":
        return cls(user_id=user_id)

    @classmethod
    def from_dict(cls, user_id: str, data: dict[str, Any]) -> "UserPreference":
        clean = dict(data)
        clean.pop("user_id", None)
        allowed = set(cls.__dataclass_fields__) - {"user_id"}  # type: ignore[attr-defined]
        return cls(user_id=user_id, **{k: v for k, v in clean.items() if k in allowed})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreferenceUpdate:
    """A single auto/manual preference field mutation."""

    field: str
    value: Any
    message: str
    source: str = "auto"
    session_id: str | None = None


_CREATE_PREFERENCES_SQL = """
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id         TEXT PRIMARY KEY,
    preference_json TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_UNDO_SQL = """
CREATE TABLE IF NOT EXISTS preference_undo_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    undo_token      TEXT NOT NULL UNIQUE,
    user_id         TEXT NOT NULL,
    session_id      TEXT,
    source          TEXT NOT NULL,
    field           TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    before_json     TEXT NOT NULL,
    after_json      TEXT NOT NULL,
    used            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


class UserMemoryStore:
    """SQLite-backed core memory store."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(_CREATE_PREFERENCES_SQL)
                conn.execute(_CREATE_UNDO_SQL)
        except Exception:  # noqa: BLE001
            logger.exception("user memory tables init failed")

    def get_preference(self, user_id: str) -> UserPreference:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT preference_json FROM user_preferences WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
        except Exception:  # noqa: BLE001
            logger.exception("failed to read preference for user %s", user_id)
            return UserPreference.empty(user_id)
        if row is None:
            return UserPreference.empty(user_id)
        try:
            data = json.loads(row["preference_json"])
        except Exception:  # noqa: BLE001
            logger.exception("invalid preference json for user %s", user_id)
            return UserPreference.empty(user_id)
        return UserPreference.from_dict(user_id, data)

    def put_preference(self, preference: UserPreference) -> UserPreference:
        payload = json.dumps(preference.to_dict(), ensure_ascii=False)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_preferences (user_id, preference_json, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                        preference_json = excluded.preference_json,
                        updated_at = excluded.updated_at
                    """,
                    (preference.user_id, payload),
                )
        except Exception:  # noqa: BLE001
            logger.exception("failed to write preference for user %s", preference.user_id)
        return preference

    def apply_update(
        self,
        user_id: str,
        update: PreferenceUpdate,
    ) -> dict[str, Any] | None:
        """Apply a high-confidence preference update and return SSE payload.

        Returns None when the update is a no-op, e.g. adding an already present
        tag. Each applied update stores a before snapshot so it can be undone.
        """
        before = self.get_preference(user_id)
        after = UserPreference.from_dict(user_id, before.to_dict())
        changed = self._apply_to_preference(after, update)
        if not changed:
            return None
        undo_token = f"undo_{secrets.token_urlsafe(16)}"
        before_json = json.dumps(before.to_dict(), ensure_ascii=False)
        after_json = json.dumps(after.to_dict(), ensure_ascii=False)
        value_json = json.dumps(update.value, ensure_ascii=False)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_preferences (user_id, preference_json, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                        preference_json = excluded.preference_json,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, after_json),
                )
                conn.execute(
                    """
                    INSERT INTO preference_undo_tokens (
                        undo_token, user_id, session_id, source, field, value_json,
                        before_json, after_json, used, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'), datetime('now'))
                    """,
                    (
                        undo_token,
                        user_id,
                        update.session_id,
                        update.source,
                        update.field,
                        value_json,
                        before_json,
                        after_json,
                    ),
                )
        except Exception:  # noqa: BLE001
            logger.exception("failed to apply preference update for user %s", user_id)
            return None
        return {
            "message": update.message,
            "field": update.field,
            "value": update.value,
            "source": update.source,
            "undo_token": undo_token,
            "preference": after.to_dict(),
        }

    def undo_update(self, user_id: str, undo_token: str) -> dict[str, Any] | None:
        """Restore the before snapshot for an unused undo token."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT before_json, field, value_json
                    FROM preference_undo_tokens
                    WHERE user_id = ? AND undo_token = ? AND used = 0
                    """,
                    (user_id, undo_token),
                ).fetchone()
                if row is None:
                    return None
                before_json = row["before_json"]
                conn.execute(
                    """
                    INSERT INTO user_preferences (user_id, preference_json, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                        preference_json = excluded.preference_json,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, before_json),
                )
                conn.execute(
                    """
                    UPDATE preference_undo_tokens
                    SET used = 1, updated_at = datetime('now')
                    WHERE user_id = ? AND undo_token = ?
                    """,
                    (user_id, undo_token),
                )
        except Exception:  # noqa: BLE001
            logger.exception("failed to undo preference update for user %s", user_id)
            return None
        try:
            value = json.loads(row["value_json"])
            preference = json.loads(before_json)
        except Exception:  # noqa: BLE001
            value = None
            preference = self.get_preference(user_id).to_dict()
        return {
            "message": "已撤销刚才记住的偏好",
            "field": row["field"],
            "value": value,
            "preference": preference,
        }

    @staticmethod
    def _apply_to_preference(preference: UserPreference, update: PreferenceUpdate) -> bool:
        field_name = update.field
        value = update.value
        if field_name in {"favorite_categories", "brand_include", "brand_exclude", "style_tags", "preference_keywords"}:
            items = getattr(preference, field_name)
            values = value if isinstance(value, list) else [value]
            changed = False
            for item in values:
                if isinstance(item, str):
                    item = item.strip()
                if item and item not in items:
                    items.append(item)
                    changed = True
            return changed
        if field_name in {"budget_min", "budget_max", "price_tier", "notes", "preference_note", "personalization_enabled"}:
            if getattr(preference, field_name) == value:
                return False
            setattr(preference, field_name, value)
            return True
        if field_name == "budget_range" and isinstance(value, dict):
            changed = False
            if preference.budget_min != value.get("budget_min"):
                preference.budget_min = value.get("budget_min")
                changed = True
            if preference.budget_max != value.get("budget_max"):
                preference.budget_max = value.get("budget_max")
                changed = True
            return changed
        return False
