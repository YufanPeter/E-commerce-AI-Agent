from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.orchestrator import Agent
from agent.intent_router import IntentDecision
from agent.session import AgentSession
from agent.tools.cart import CartTool, detect_order_intent
from store.cart_store import CartNotFoundError, CartStore, DEMO_USER_ID
from store.order_store import OrderStore


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
def db(tmp_path: Path) -> Path:
    path = tmp_path / "t.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO products(product_id,title,brand,category,sub_category,base_price) VALUES (?,?,?,?,?,?)",
        [
            ("p1", "珊珂绵润洁面乳", "珊珂", "美妆护肤", "洁面", 52),
            ("p2", "兰蔻小黑瓶", "兰蔻", "美妆护肤", "精华", 760),
        ],
    )
    conn.executemany(
        "INSERT INTO product_skus(sku_id,product_id,properties_json,price) VALUES (?,?,?,?)",
        [
            ("p1_s1", "p1", '{"容量": "120g"}', 52),
            ("p1_s2", "p1", '{"容量": "50g"}', 30),
            ("p2_s1", "p2", '{"容量": "30ml"}', 760),
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def store(db: Path) -> CartStore:
    return CartStore(db)


@pytest.fixture()
def tool(store: CartStore) -> CartTool:
    return CartTool(cart_store=store)


def _session(text: str, user_id: str | None = "u1") -> AgentSession:
    session = AgentSession(user_id=user_id)
    session.add_user(text)
    return session


def _place(store: CartStore, *items: tuple[str, str, int], user: str = "u1"):
    for product_id, sku_id, quantity in items:
        store.add_product(product_id, sku_id=sku_id, quantity=quantity)
    return store.build_order(order_user_id=user)


def _set_price(db: Path, sku_id: str, price: float) -> None:
    conn = sqlite3.connect(db)
    conn.execute("UPDATE product_skus SET price = ? WHERE sku_id = ?", (price, sku_id))
    conn.commit()
    conn.close()


def _set_status(db: Path, sku_id: str, status: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute("UPDATE product_skus SET status = ? WHERE sku_id = ?", (status, sku_id))
    conn.commit()
    conn.close()


# --------------------------- 持久化 ---------------------------

def test_order_is_persisted_with_snapshot_and_cart_cleared(store: CartStore, db: Path):
    order = _place(store, ("p1", "p1_s1", 2))

    assert store.list_items() == []
    saved = OrderStore(db).list_orders("u1")
    assert saved[0]["order_id"] == order.order_id
    assert saved[0]["total"] == 104
    line = saved[0]["lines"][0]
    assert (line["title"], line["brand"], line["quantity"], line["unit_price"]) == ("珊珂绵润洁面乳", "珊珂", 2, 52)
    assert line["options"] == {"容量": "120g"}


def test_snapshot_survives_price_change(store: CartStore, db: Path):
    _place(store, ("p1", "p1_s1", 1))
    _set_price(db, "p1_s1", 99)
    assert OrderStore(db).list_orders("u1")[0]["lines"][0]["unit_price"] == 52


def test_orders_are_scoped_to_the_buyer(store: CartStore, db: Path):
    _place(store, ("p1", "p1_s1", 1), user="u1")
    assert OrderStore(db).list_orders("u2") == []


def test_orders_are_newest_first(store: CartStore, db: Path):
    first = _place(store, ("p1", "p1_s1", 1))
    second = _place(store, ("p2", "p2_s1", 1))
    assert [o["order_id"] for o in OrderStore(db).list_orders("u1")] == [second.order_id, first.order_id]


def test_order_uses_current_price_not_stale_cart_price(store: CartStore, db: Path):
    store.add_product("p1", sku_id="p1_s1")
    _set_price(db, "p1_s1", 40)
    assert store.build_order(order_user_id="u1").total == 40


def test_failed_order_keeps_cart_and_writes_nothing(store: CartStore, db: Path):
    store.add_product("p1", sku_id="p1_s1")
    _set_status(db, "p1_s1", "offline")
    with pytest.raises(CartNotFoundError):
        store.build_order(order_user_id="u1")
    assert len(store.list_items()) == 1
    assert OrderStore(db).list_orders("u1") == []


def test_only_requested_lines_are_ordered(store: CartStore, db: Path):
    keep = store.add_product("p1", sku_id="p1_s1")
    buy = store.add_product("p2", sku_id="p2_s1")
    store.build_order(order_user_id="u1", cart_item_ids=[buy.cart_item_id])
    assert [line.cart_item_id for line in store.list_items()] == [keep.cart_item_id]
    assert OrderStore(db).list_orders("u1")[0]["total"] == 760


def test_empty_cart_cannot_order(store: CartStore):
    with pytest.raises(CartNotFoundError):
        store.build_order(order_user_id="u1")


# --------------------------- 意图 ---------------------------

@pytest.mark.parametrize("text", ["再买一次", "上次买的洗面奶再来两瓶", "把上一单再下一遍", "补货", "之前买的那个再来一份"])
def test_reorder_intent(text):
    assert detect_order_intent(text) == "reorder"


@pytest.mark.parametrize("text", ["我买过什么", "上次买的是什么", "看看历史订单", "我的订单"])
def test_history_intent(text):
    assert detect_order_intent(text) == "history"


@pytest.mark.parametrize("text", ["上次买的洗面奶怎么样", "推荐洗面奶", "买什么好", "上次买的多少钱", "第一个加入购物车"])
def test_not_order_intent(text):
    assert detect_order_intent(text) is None


def test_router_mistake_is_corrected_to_cart():
    wrong = IntentDecision(tool="recommend", rewritten_query="洗面奶", confidence="high", reasoning="")
    fixed = Agent._apply_post_route_guards("上次买的洗面奶再来两瓶", AgentSession(), wrong)
    assert fixed.tool == "cart"


# --------------------------- 再买一次 ---------------------------

def test_reorder_adds_same_sku_with_original_quantity_and_does_not_order(tool, store: CartStore, db: Path):
    _place(store, ("p1", "p1_s2", 3))
    result = tool.run("再买一次", _session("再买一次"), {})

    line = store.list_items()[0]
    assert (line.sku_id, line.quantity) == ("p1_s2", 3)
    assert len(OrderStore(db).list_orders("u1")) == 1
    assert "要直接下单吗" in result.narrative_override
    assert result.payload["action"] == "add"


def test_reorder_quantity_comes_from_the_sentence(tool, store: CartStore):
    _place(store, ("p1", "p1_s1", 1))
    tool.run("再买一次，来两瓶", _session("再买一次，来两瓶"), {})
    assert store.list_items()[0].quantity == 2


def test_ordinal_is_not_a_quantity():
    from agent.tools.cart import _quantity_in

    assert _quantity_in("第二个") is None
    assert _quantity_in("来两瓶") == 2
    assert _quantity_in("再买3件") == 3


def test_reorder_reports_price_change(tool, store: CartStore, db: Path):
    _place(store, ("p1", "p1_s1", 1))
    _set_price(db, "p1_s1", 45)
    result = tool.run("再买一次", _session("再买一次"), {})
    assert "比上次便宜 ¥7" in result.narrative_override
    assert store.list_items()[0].unit_price == 45


def test_reorder_by_name_picks_from_history(tool, store: CartStore):
    _place(store, ("p1", "p1_s1", 1), ("p2", "p2_s1", 1))
    tool.run("上次买的兰蔻再来一瓶", _session("上次买的兰蔻再来一瓶"), {})
    assert [line.product_id for line in store.list_items()] == ["p2"]


def test_reorder_ambiguous_asks_then_uses_answer(tool, store: CartStore):
    _place(store, ("p1", "p1_s1", 1), ("p2", "p2_s1", 1))
    session = _session("再买一次")
    asked = tool.run("再买一次", session, {})
    assert "想再买哪一个" in asked.narrative_override
    assert store.list_items() == []

    session.add_user("第二个")
    tool.run("第二个", session, {})
    assert [(line.product_id, line.quantity) for line in store.list_items()] == [("p2", 1)]
    assert session.get("pending_add") is None


def test_reorder_whole_last_order(tool, store: CartStore):
    _place(store, ("p1", "p1_s1", 2), ("p2", "p2_s1", 1))
    result = tool.run("把上一单再下一遍", _session("把上一单再下一遍"), {})
    assert sorted((l.product_id, l.quantity) for l in store.list_items()) == [("p1", 2), ("p2", 1)]
    assert result.payload["action"] == "reorder"


def test_reorder_unknown_product_lists_history_instead_of_guessing(tool, store: CartStore, monkeypatch):
    _place(store, ("p1", "p1_s1", 1), ("p2", "p2_s1", 1))
    monkeypatch.setattr("agent.tools.cart.resolve_one", lambda *a, **k: None)
    result = tool.run("上次买的跑鞋再来一双", _session("上次买的跑鞋再来一双"), {})
    assert "没找到" in result.narrative_override
    assert store.list_items() == []


def test_reorder_delisted_sku_asks_for_another_spec(tool, store: CartStore, db: Path):
    _place(store, ("p1", "p1_s1", 1))
    _set_status(db, "p1_s1", "offline")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO product_skus(sku_id,product_id,properties_json,price) VALUES (?,?,?,?)", ("p1_s3", "p1", '{"容量": "80g"}', 40))
    conn.commit()
    conn.close()
    result = tool.run("再买一次", _session("再买一次"), {})
    assert "下架" in result.narrative_override
    assert store.list_items() == []


def test_reorder_without_history(tool):
    assert "下单记录" in tool.run("再买一次", _session("再买一次"), {}).narrative_override


def test_history_lists_orders_and_enables_references(tool, store: CartStore):
    _place(store, ("p1", "p1_s1", 2))
    session = _session("我买过什么")
    result = tool.run("我买过什么", session, {})
    assert "珊珂" in result.narrative_override
    assert session.recall_hits()[0]["product_id"] == "p1"


def test_anonymous_session_uses_demo_owner(tool, store: CartStore, db: Path):
    _place(store, ("p1", "p1_s1", 1), user=DEMO_USER_ID)
    tool.run("再买一次", _session("再买一次", user_id=None), {})
    assert store.list_items()[0].product_id == "p1"


def test_chat_checkout_is_saved_under_the_user(tool, store: CartStore, db: Path, monkeypatch):
    store.add_product("p1", sku_id="p1_s1")
    from agent.llm_actions import ActionDecision

    monkeypatch.setattr("agent.tools.cart.dispatch_action", lambda *a, **k: ActionDecision("checkout", {}))
    tool.run("下单吧", _session("下单吧"), {})
    assert OrderStore(db).list_orders("u1")[0]["total"] == 52
