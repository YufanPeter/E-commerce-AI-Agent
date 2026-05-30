"""对话式购物车 + 下单 + 统一工具调用层的回归测试。

不打真实 LLM（dispatch 用 monkeypatch 注入动作决策），不碰真实 SQLite
（CartStore 用临时库 + 直接建表），保证 CI 可无脑跑。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.llm_actions import ActionDecision
from agent.session import AgentSession
from agent.tools import cart as cart_module
from agent.tools.cart import CartTool
from store.cart_store import CartStore


# --------------------------- 临时 SQLite 夹具 ---------------------------

_SCHEMA = """
CREATE TABLE products (
    product_id TEXT PRIMARY KEY, title TEXT, brand TEXT,
    category TEXT, sub_category TEXT, base_price REAL, status TEXT DEFAULT 'active'
);
CREATE TABLE product_skus (
    sku_id TEXT PRIMARY KEY, product_id TEXT, properties_json TEXT DEFAULT '{}',
    price REAL, stock_qty INTEGER DEFAULT 99, status TEXT DEFAULT 'active'
);
CREATE TABLE cart_items (
    cart_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT, product_id TEXT, sku_id TEXT,
    selected_options_json TEXT DEFAULT '{}', quantity INTEGER,
    unit_price REAL, selected INTEGER DEFAULT 1,
    UNIQUE(user_id, sku_id)
);
"""


@pytest.fixture()
def store(tmp_path: Path) -> CartStore:
    db = tmp_path / "t.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO products(product_id,title,brand,category,sub_category,base_price) VALUES (?,?,?,?,?,?)",
        [
            ("p1", "珀莱雅双抗精华", "珀莱雅", "美妆护肤", "精华", 129),
            ("p2", "兰蔻小黑瓶", "兰蔻", "美妆护肤", "精华", 680),
        ],
    )
    conn.executemany(
        "INSERT INTO product_skus(sku_id,product_id,price) VALUES (?,?,?)",
        [("p1_s1", "p1", 129), ("p2_s1", "p2", 680)],
    )
    conn.commit()
    conn.close()
    return CartStore(db)


def _session_with_hits() -> AgentSession:
    sess = AgentSession()
    sess.remember_search(
        {"category": "美妆护肤"},
        [{"product_id": "p1", "title": "珀莱雅双抗精华"},
         {"product_id": "p2", "title": "兰蔻小黑瓶"}],
    )
    return sess


def _stub_dispatch(action: str, **args):
    """替换 dispatch_action，固定返回某动作，绕开真实 LLM。"""
    def _fn(query, session, actions, *, purpose, timeout=6.0):
        return ActionDecision(action=action, args=args, raw={"action": action, **args})
    return _fn


# --------------------------- CartStore 直测 ---------------------------


def test_store_add_and_list(store: CartStore):
    store.add_product("p1")
    store.add_product("p1")  # 累加
    lines = store.list_items()
    assert len(lines) == 1
    assert lines[0].quantity == 2
    assert lines[0].subtotal == 258


def test_store_set_quantity_zero_removes(store: CartStore):
    line = store.add_product("p1")
    assert store.set_quantity(line.cart_item_id, 0) is None
    assert store.list_items() == []


def test_store_build_order_clears(store: CartStore):
    store.add_product("p1", quantity=2)
    store.add_product("p2")
    order = store.build_order()
    assert order.total == 129 * 2 + 680
    assert order.to_dict()["item_count"] == 3
    assert store.list_items() == []  # 下单后清空


def test_store_checkout_empty_raises(store: CartStore):
    from store.cart_store import CartNotFoundError
    with pytest.raises(CartNotFoundError):
        store.build_order()


# --------------------------- CartTool + 统一调用层 ---------------------------


def test_cart_add_resolves_index(monkeypatch, store: CartStore):
    monkeypatch.setattr(cart_module, "dispatch_action", _stub_dispatch("add", index=2))
    tool = CartTool(cart_store=store)
    r = tool.run("把第二个加进来", _session_with_hits(), {})
    assert r.payload["action"] == "add"
    assert r.payload["added"]["title"] == "兰蔻小黑瓶"
    assert r.payload["cart"]["item_count"] == 1


def test_cart_add_uses_focus_when_no_index(monkeypatch, store: CartStore):
    monkeypatch.setattr(cart_module, "dispatch_action", _stub_dispatch("add"))
    sess = _session_with_hits()
    sess.set("last_focus_product_id", "p2")  # 上一轮 detail 聚焦了第二款
    r = CartTool(cart_store=store).run("把刚才那款加进来", sess, {})
    assert r.payload["added"]["product_id"] == "p2"


def test_cart_remove_by_cart_index(monkeypatch, store: CartStore):
    store.add_product("p1")
    store.add_product("p2")
    monkeypatch.setattr(cart_module, "dispatch_action", _stub_dispatch("remove", cart_index=2))
    r = CartTool(cart_store=store).run("删掉第二个", AgentSession(), {})
    assert r.payload["removed"]["title"] == "兰蔻小黑瓶"
    assert r.payload["cart"]["item_count"] == 1


def test_cart_set_quantity(monkeypatch, store: CartStore):
    store.add_product("p1")
    monkeypatch.setattr(cart_module, "dispatch_action", _stub_dispatch("set_quantity", cart_index=1, quantity=3))
    r = CartTool(cart_store=store).run("第一件改成3个", AgentSession(), {})
    assert r.payload["cart"]["lines"][0]["quantity"] == 3


def test_cart_checkout_default_address(monkeypatch, store: CartStore):
    store.add_product("p1", quantity=2)
    monkeypatch.setattr(cart_module, "dispatch_action", _stub_dispatch("checkout"))
    r = CartTool(cart_store=store).run("下单吧，地址用默认的", AgentSession(), {})
    assert r.payload["action"] == "checkout"
    assert r.payload["order"]["total"] == 258
    assert "默认地址" in r.payload["order"]["address"]
    assert store.list_items() == []  # 业务闭环：下单后购物车清空


def test_cart_view_empty(monkeypatch, store: CartStore):
    monkeypatch.setattr(cart_module, "dispatch_action", _stub_dispatch("view"))
    r = CartTool(cart_store=store).run("看看购物车", AgentSession(), {})
    assert r.needs_composer is False
    assert "空的" in r.narrative_override


def test_cart_dispatch_failure_falls_back_to_view(monkeypatch, store: CartStore):
    def _boom(*a, **k):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(cart_module, "dispatch_action", _boom)
    store.add_product("p1")
    r = CartTool(cart_store=store).run("随便说点啥", AgentSession(), {})
    # 降级为展示购物车，而非报错
    assert r.payload["action"] == "view"
