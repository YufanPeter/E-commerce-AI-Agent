from __future__ import annotations

"""SqliteSessionStore 持久化测试（用临时 DB，不触网）。"""

import sqlite3

import pytest

from agent.session import AgentSession
from store.session_store import SqliteSessionStore


@pytest.fixture
def store(tmp_path):
    # agent_sessions 懒建表，所以空 DB 文件即可
    db = tmp_path / "test.sqlite3"
    sqlite3.connect(db).close()
    return SqliteSessionStore(db_path=db)


def test_save_and_reload_history(store, tmp_path):
    sess = store.get_or_create("s1")
    sess.add_user("买手机")
    sess.add_assistant("推荐几款")
    store.save(sess)

    # 新建 store 实例 = 模拟后端重启
    reloaded_store = SqliteSessionStore(db_path=store.db_path)
    loaded = reloaded_store.get_or_create("s1")
    assert [(m.role, m.content) for m in loaded.history] == [
        ("user", "买手机"),
        ("assistant", "推荐几款"),
    ]


def test_working_memory_persists(store):
    sess = store.get_or_create("s2")
    sess.set("last_hits", [{"product_id": "p1", "title": "A"}])
    sess.set("pending_cart", {"product_id": "p1", "title": "A", "quantity": 1})
    store.save(sess)

    loaded = SqliteSessionStore(db_path=store.db_path).get_or_create("s2")
    assert loaded.get("last_hits") == [{"product_id": "p1", "title": "A"}]
    assert loaded.get("pending_cart")["product_id"] == "p1"


def test_get_or_create_new_session_is_empty(store):
    sess = store.get_or_create("brand_new")
    assert sess.history == []
    assert sess.working_memory == {}


def test_reset_removes_session(store):
    sess = store.get_or_create("s3")
    sess.add_user("hi")
    store.save(sess)
    store.reset("s3")
    # reset 后再取应是空会话
    loaded = SqliteSessionStore(db_path=store.db_path).get_or_create("s3")
    assert loaded.history == []


def test_save_handles_unserializable_memory_gracefully(store):
    sess = store.get_or_create("s4")
    sess.set("bad", object())  # 不可 JSON 序列化
    # 不应抛异常；memory 降级存空
    store.save(sess)
    loaded = SqliteSessionStore(db_path=store.db_path).get_or_create("s4")
    assert loaded.working_memory == {}
