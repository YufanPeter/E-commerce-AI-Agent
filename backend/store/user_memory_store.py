from __future__ import annotations

"""User memory persistence.

两层结构，互相对齐：

- ``user_preferences.preference_json``：检索和接口消费的当前偏好（``UserPreference``）。
- ``memory_items``：每一条记忆的台账，带来源、原话、置信度、使用次数和状态。页面展示、
  确认、驳回、删除都针对条目；偏好 JSON 变化后由 ``_sync_items`` 对齐，所以旧接口
  （PUT /preferences、撤销）写入的内容也会出现在台账里。

``user_events`` 记录行为事件（搜索、看详情、对比、加购、下单），只用于推断偏好建议和
展示最近活动。推断结果只进入 ``suggested`` 状态，用户确认前不会写进偏好。
"""

import json
import logging
import secrets
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from store.product_store import DEFAULT_DB_PATH


logger = logging.getLogger(__name__)


LEGACY_CATEGORY_NAMES = {"食品生活": "食品饮料"}


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
    # 成分 / 食材忌讳（酒精、花生…）：只有用户明确确认后才进来，是硬过滤。
    avoid_terms: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls, user_id: str) -> "UserPreference":
        return cls(user_id=user_id)

    @classmethod
    def from_dict(cls, user_id: str, data: dict[str, Any]) -> "UserPreference":
        clean = dict(data)
        clean.pop("user_id", None)
        allowed = set(cls.__dataclass_fields__) - {"user_id"}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in clean.items() if k in allowed}
        # 旧版页面和抽取器写的是「食品生活」，库里真实类目是「食品饮料」。
        if kwargs.get("favorite_categories"):
            kwargs["favorite_categories"] = list(dict.fromkeys(
                LEGACY_CATEGORY_NAMES.get(c, c) for c in kwargs["favorite_categories"]
            ))
        return cls(user_id=user_id, **kwargs)

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
    evidence: str | None = None          # 用户原话，页面展示「你说过」用
    confidence: float = 1.0


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


_CREATE_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS memory_items (
    item_id      TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT '',
    attr         TEXT NOT NULL DEFAULT '',
    value        TEXT NOT NULL,
    status       TEXT NOT NULL,
    source       TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 1.0,
    evidence     TEXT,
    session_id   TEXT,
    use_count    INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, kind, scope, attr, value)
)
"""

_CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS user_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    session_id   TEXT,
    type         TEXT NOT NULL,
    product_id   TEXT,
    title        TEXT,
    brand        TEXT,
    category     TEXT,
    sub_category TEXT,
    price        REAL,
    payload_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# One row per conversation: what it was about, so a later chat can recall or pick it up.
# ``facts_json`` is rebuilt from the conversation after every turn; ``title`` and ``summary``
# start as plain facts and are rewritten in the background by the model.
_CREATE_DIGESTS_SQL = """
CREATE TABLE IF NOT EXISTS conversation_digests (
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    facts_json  TEXT NOT NULL,
    turns       INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, session_id)
)
"""

_CREATE_EVENTS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_user_events_user_time ON user_events (user_id, created_at)"
)

# 条目状态
ACTIVE = "active"          # 生效中，已写进偏好
SUGGESTED = "suggested"    # 等用户确认，不写进偏好
REJECTED = "rejected"      # 用户说不对，不再提示
REMOVED = "removed"        # 用户删除，撤销时可恢复

# 属性键 → 展示名
ATTR_LABELS = {
    "skin_type": "肤质",
    "shoe_size": "鞋码",
    "clothing_size": "衣服尺码",
    "household": "家庭情况",
}


@dataclass(frozen=True)
class MemoryKey:
    """一条记忆的身份：同一身份在台账里只有一行。"""

    kind: str
    scope: str = ""
    attr: str = ""
    value: str = ""


def _format_money(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _budget_value(low: float | None, high: float | None) -> str:
    return f"{_format_money(low)}-{_format_money(high)}"


def flatten_preference(preference: "UserPreference") -> list[MemoryKey]:
    """把偏好 JSON 展开成条目身份，是台账对齐和删除的共同基础。"""
    keys: list[MemoryKey] = []
    if preference.budget_min is not None or preference.budget_max is not None:
        keys.append(MemoryKey("budget", value=_budget_value(preference.budget_min, preference.budget_max)))
    if preference.price_tier and preference.price_tier != "balanced":
        keys.append(MemoryKey("price_tier", value=preference.price_tier))
    keys += [MemoryKey("brand_like", value=v) for v in preference.brand_include]
    keys += [MemoryKey("brand_avoid", value=v) for v in preference.brand_exclude]
    keys += [MemoryKey("category", value=v) for v in preference.favorite_categories]
    keys += [MemoryKey("avoid_term", value=v) for v in preference.avoid_terms]
    styles = list(dict.fromkeys([*preference.preference_keywords, *preference.style_tags]))
    keys += [MemoryKey("style", value=v) for v in styles]
    note = (preference.preference_note or preference.notes or "").strip()
    if note:
        keys.append(MemoryKey("note", value=note))
    for scope, attrs in (preference.category_specific or {}).items():
        if not isinstance(attrs, dict):
            continue
        if attrs.get("budget_min") is not None or attrs.get("budget_max") is not None:
            keys.append(MemoryKey("budget", scope=scope, value=_budget_value(attrs.get("budget_min"), attrs.get("budget_max"))))
        for attr, value in attrs.items():
            if attr in {"budget_min", "budget_max"} or value in (None, ""):
                continue
            keys.append(MemoryKey("attribute", scope=scope, attr=attr, value=str(value)))
    return keys


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
                conn.execute(_CREATE_ITEMS_SQL)
                conn.execute(_CREATE_EVENTS_SQL)
                conn.execute(_CREATE_EVENTS_INDEX_SQL)
                conn.execute(_CREATE_DIGESTS_SQL)
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
        self._sync_items(preference.user_id, preference, source="manual")
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
        self._sync_items(
            user_id,
            after,
            source="stated" if update.source == "auto" else update.source,
            evidence=update.evidence,
            session_id=update.session_id,
            confidence=update.confidence,
        )
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
        self._sync_items(user_id, UserPreference.from_dict(user_id, json.loads(before_json)), source="manual")
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
        if field_name in {"favorite_categories", "brand_include", "brand_exclude", "style_tags", "preference_keywords", "avoid_terms"}:
            items = getattr(preference, field_name)
            values = value if isinstance(value, list) else [value]
            changed = False
            # 「我喜欢苹果」和「不买苹果」不能同时成立：后说的覆盖先说的。
            opposite = {"brand_include": preference.brand_exclude, "brand_exclude": preference.brand_include}.get(field_name)
            for item in values:
                if isinstance(item, str):
                    item = item.strip()
                if opposite is not None and item in opposite:
                    opposite.remove(item)
                    changed = True
                if item and item not in items:
                    items.append(item)
                    changed = True
            return changed
        if field_name == "category_attr" and isinstance(value, dict):
            bucket = preference.category_specific.setdefault(str(value.get("category") or ""), {})
            if bucket.get(value.get("key")) == value.get("value"):
                return False
            bucket[str(value.get("key"))] = value.get("value")
            return True
        if field_name == "category_budget" and isinstance(value, dict):
            bucket = preference.category_specific.setdefault(str(value.get("category") or ""), {})
            changed = False
            for key in ("budget_min", "budget_max"):
                if bucket.get(key) != value.get(key):
                    bucket[key] = value.get(key)
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

    # ------------------------------------------------------------------
    # 台账
    # ------------------------------------------------------------------

    def _sync_items(
        self,
        user_id: str,
        preference: UserPreference,
        *,
        source: str,
        evidence: str | None = None,
        session_id: str | None = None,
        confidence: float = 1.0,
    ) -> None:
        """让台账和偏好 JSON 对齐：新值建条目，消失的值标记删除，恢复的值保留原来源。"""
        keys = flatten_preference(preference)
        wanted = {(k.kind, k.scope, k.attr, k.value) for k in keys}
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT item_id, kind, scope, attr, value, status FROM memory_items WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                existing = {(r["kind"], r["scope"], r["attr"], r["value"]): r for r in rows}
                for key in keys:
                    identity = (key.kind, key.scope, key.attr, key.value)
                    row = existing.get(identity)
                    if row is None:
                        conn.execute(
                            """
                            INSERT INTO memory_items (
                                item_id, user_id, kind, scope, attr, value, status,
                                source, confidence, evidence, session_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (uuid4().hex, user_id, *identity, ACTIVE, source, confidence, evidence, session_id),
                        )
                    elif row["status"] != ACTIVE:
                        conn.execute(
                            "UPDATE memory_items SET status = ?, updated_at = datetime('now') WHERE item_id = ?",
                            (ACTIVE, row["item_id"]),
                        )
                for identity, row in existing.items():
                    if row["status"] == ACTIVE and identity not in wanted:
                        conn.execute(
                            "UPDATE memory_items SET status = ?, updated_at = datetime('now') WHERE item_id = ?",
                            (REMOVED, row["item_id"]),
                        )
        except Exception:  # noqa: BLE001 - 台账失败不能影响偏好本身
            logger.exception("failed to sync memory items for user %s", user_id)

    def list_items(self, user_id: str, statuses: tuple[str, ...] = (ACTIVE, SUGGESTED)) -> list[dict[str, Any]]:
        # 旧数据只有偏好 JSON：读之前对齐一次，老用户打开页面也能看到自己的记忆。
        self._sync_items(user_id, self.get_preference(user_id), source="manual")
        marks = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM memory_items WHERE user_id = ? AND status IN ({marks}) "
                "ORDER BY updated_at DESC, created_at DESC",
                (user_id, *statuses),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_item(self, user_id: str, item_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            ).fetchone()
        return dict(row) if row else None

    def suggest_item(
        self,
        user_id: str,
        key: MemoryKey,
        *,
        source: str,
        evidence: str | None,
        confidence: float,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """登记一条待确认建议。已生效、已驳回、已删除的同一身份都不再提示。"""
        identity = (key.kind, key.scope, key.attr, key.value)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item_id, status FROM memory_items "
                "WHERE user_id = ? AND kind = ? AND scope = ? AND attr = ? AND value = ?",
                (user_id, *identity),
            ).fetchone()
            if row is not None:
                if row["status"] == SUGGESTED:
                    conn.execute(
                        "UPDATE memory_items SET evidence = ?, confidence = ?, updated_at = datetime('now') WHERE item_id = ?",
                        (evidence, confidence, row["item_id"]),
                    )
                    return self.get_item(user_id, row["item_id"])
                return None
            item_id = uuid4().hex
            conn.execute(
                """
                INSERT INTO memory_items (
                    item_id, user_id, kind, scope, attr, value, status,
                    source, confidence, evidence, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, user_id, *identity, SUGGESTED, source, confidence, evidence, session_id),
            )
        return self.get_item(user_id, item_id)

    def retract_suggestion(self, user_id: str, key: MemoryKey) -> None:
        """行为信号变弱后，撤掉还没被用户处理的推断建议。"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM memory_items WHERE user_id = ? AND kind = ? AND scope = ? AND attr = ? "
                "AND value = ? AND status = ? AND source = 'inferred'",
                (user_id, key.kind, key.scope, key.attr, key.value, SUGGESTED),
            )

    def set_item_status(self, user_id: str, item_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE memory_items SET status = ?, updated_at = datetime('now') WHERE user_id = ? AND item_id = ?",
                (status, user_id, item_id),
            )

    def mark_confirmed(self, user_id: str, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE memory_items SET status = ?, source = 'confirmed', updated_at = datetime('now') "
                "WHERE user_id = ? AND item_id = ?",
                (ACTIVE, user_id, item_id),
            )

    def remove_item(self, user_id: str, item_id: str) -> dict[str, Any] | None:
        """删除一条生效记忆，同步更新偏好，返回撤销 token。"""
        item = self.get_item(user_id, item_id)
        if item is None:
            return None
        before = self.get_preference(user_id)
        after = UserPreference.from_dict(user_id, before.to_dict())
        _remove_key_from_preference(after, MemoryKey(item["kind"], item["scope"], item["attr"], item["value"]))
        undo_token = f"undo_{secrets.token_urlsafe(16)}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_preferences (user_id, preference_json, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    preference_json = excluded.preference_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(after.to_dict(), ensure_ascii=False)),
            )
            conn.execute(
                """
                INSERT INTO preference_undo_tokens (
                    undo_token, user_id, session_id, source, field, value_json,
                    before_json, after_json, used, created_at, updated_at
                )
                VALUES (?, ?, NULL, 'manual', ?, ?, ?, ?, 0, datetime('now'), datetime('now'))
                """,
                (
                    undo_token,
                    user_id,
                    item["kind"],
                    json.dumps(item["value"], ensure_ascii=False),
                    json.dumps(before.to_dict(), ensure_ascii=False),
                    json.dumps(after.to_dict(), ensure_ascii=False),
                ),
            )
        self._sync_items(user_id, after, source="manual")
        return {"undo_token": undo_token, "item": item}

    def mark_used(self, user_id: str, keys: list[MemoryKey]) -> None:
        if not keys:
            return
        with self._connect() as conn:
            for key in keys:
                conn.execute(
                    """
                    UPDATE memory_items
                    SET use_count = use_count + 1, last_used_at = datetime('now')
                    WHERE user_id = ? AND kind = ? AND scope = ? AND attr = ? AND value = ?
                    """,
                    (user_id, key.kind, key.scope, key.attr, key.value),
                )

    def clear_user(self, user_id: str) -> None:
        """彻底忘掉一个用户：偏好、台账、事件、撤销记录全部删除。"""
        with self._connect() as conn:
            for table in ("user_preferences", "memory_items", "user_events", "preference_undo_tokens", "conversation_digests"):
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))

    # ------------------------------------------------------------------
    # 行为事件
    # ------------------------------------------------------------------

    def record_events(self, user_id: str, session_id: str | None, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO user_events (
                        user_id, session_id, type, product_id, title, brand,
                        category, sub_category, price, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            user_id, session_id, ev["type"], ev.get("product_id"), ev.get("title"),
                            ev.get("brand"), ev.get("category"), ev.get("sub_category"), ev.get("price"),
                            json.dumps(ev.get("payload") or {}, ensure_ascii=False),
                        )
                        for ev in events
                    ],
                )
        except Exception:  # noqa: BLE001 - 事件丢失不影响对话
            logger.exception("failed to record events for user %s", user_id)

    def recent_events(self, user_id: str, days: int = 90, limit: int = 400) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM user_events
                WHERE user_id = ? AND created_at >= datetime('now', ?)
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (user_id, f"-{int(days)} days", limit),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            out.append(item)
        return out


    def session_events(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        """Events of one conversation, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT type, product_id, title, sub_category, payload_json FROM user_events "
                "WHERE user_id = ? AND session_id = ? ORDER BY id",
                (user_id, session_id),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Conversation digests
    # ------------------------------------------------------------------

    def save_digest_facts(self, user_id: str, session_id: str, title: str, summary: str, facts: dict[str, Any], turns: int) -> None:
        """Insert a conversation or refresh its facts; a title and summary already written stay."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_digests (user_id, session_id, title, summary, facts_json, turns)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, session_id) DO UPDATE SET
                    facts_json = excluded.facts_json,
                    turns      = excluded.turns,
                    updated_at = datetime('now')
                """,
                (user_id, session_id, title, summary, json.dumps(facts, ensure_ascii=False), turns),
            )

    def save_digest_text(self, user_id: str, session_id: str, title: str, summary: str, turns: int) -> None:
        """The model's title and summary, kept only while no later turn has arrived."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversation_digests SET title = ?, summary = ? WHERE user_id = ? AND session_id = ? AND turns = ?",
                (title, summary, user_id, session_id, turns),
            )

    def list_digests(self, user_id: str, limit: int = 20, exclude_session: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversation_digests
                WHERE user_id = ? AND session_id <> ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (user_id, exclude_session or "", limit),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["facts"] = json.loads(item.pop("facts_json") or "{}")
            except json.JSONDecodeError:
                item["facts"] = {}
            out.append(item)
        return out

    def delete_digest(self, user_id: str, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM conversation_digests WHERE user_id = ? AND session_id = ?", (user_id, session_id),
            )
        return cursor.rowcount > 0


def _remove_key_from_preference(preference: UserPreference, key: MemoryKey) -> None:
    if key.kind == "budget" and not key.scope:
        preference.budget_min = None
        preference.budget_max = None
    elif key.kind == "budget":
        bucket = preference.category_specific.get(key.scope) or {}
        bucket.pop("budget_min", None)
        bucket.pop("budget_max", None)
        if not bucket:
            preference.category_specific.pop(key.scope, None)
    elif key.kind == "attribute":
        bucket = preference.category_specific.get(key.scope) or {}
        bucket.pop(key.attr, None)
        if not bucket:
            preference.category_specific.pop(key.scope, None)
    elif key.kind == "price_tier":
        preference.price_tier = None
    elif key.kind == "note":
        preference.preference_note = None
        preference.notes = None
    elif key.kind == "style":
        preference.preference_keywords = [v for v in preference.preference_keywords if v != key.value]
        preference.style_tags = [v for v in preference.style_tags if v != key.value]
    else:
        field_name = {
            "brand_like": "brand_include",
            "brand_avoid": "brand_exclude",
            "category": "favorite_categories",
            "avoid_term": "avoid_terms",
        }.get(key.kind)
        if field_name:
            setattr(preference, field_name, [v for v in getattr(preference, field_name) if v != key.value])
