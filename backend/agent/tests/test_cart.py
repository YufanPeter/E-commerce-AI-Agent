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


def test_store_clear_removes_all(store: CartStore):
    store.add_product("p1", quantity=2)
    store.add_product("p2")
    removed = store.clear()
    assert removed == 2  # 两个 SKU 行被删
    assert store.list_items() == []


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


# --------------------------- 规格消歧（共享前缀防死循环）---------------------------

def _phone_skus() -> list[dict]:
    """模拟 Pura 90 Pro：存储 12GB+256GB(标准版) / 12GB+512GB(高配版)，共享 12GB 前缀。"""
    skus = []
    for storage, version, colors in (
        ("12GB+256GB", "标准版", ["星芒黑", "雪域白", "幻影紫", "青釉绿"]),
        ("12GB+512GB", "高配版", ["星芒黑", "雪域白", "幻影紫", "青釉绿"]),
    ):
        for i, color in enumerate(colors):
            skus.append({
                "sku_id": f"{storage}_{color}",
                "options": {"存储": storage, "颜色": color, "版本": version},
            })
    return skus


def test_filter_skus_disambiguates_shared_numeric_prefix(store: CartStore):
    """用户说 12GB+512GB 不能被 12GB+256GB 抢匹配（两者共享 '12'）。"""
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("我要 12GB+512GB 幻影紫 高配版", _phone_skus())
    assert len(matched) == 1
    assert matched[0]["options"] == {"存储": "12GB+512GB", "颜色": "幻影紫", "版本": "高配版"}


def test_filter_skus_lower_storage_still_works(store: CartStore):
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("我要 12GB+256GB 星芒黑 标准版", _phone_skus())
    assert len(matched) == 1
    assert matched[0]["options"]["存储"] == "12GB+256GB"


def test_value_match_score_prefers_more_complete_match(store: CartStore):
    tool = CartTool(cart_store=store)
    q = "我要 12GB+512GB"
    # 512 版本命中 12+512=2 个 token，256 版本仅命中 12=1 个 token
    assert tool._value_match_score("12GB+512GB", q) > tool._value_match_score("12GB+256GB", q)


def _pants_skus() -> list[dict]:
    """优衣库短裤：尺码 M/L/XL 码 × 颜色，字母尺码无数字、中文'码'仅 1 字。"""
    skus = []
    for size in ("M码", "L码", "XL码"):
        for color in ("黑色", "深灰色", "藏蓝色"):
            skus.append({"sku_id": f"{size}_{color}", "options": {"尺码": size, "颜色": color}})
    return skus


def test_filter_skus_letter_size_resolves_uniquely(store: CartStore):
    """字母尺码 M码 + 深灰色应唯一命中，不再因尺码识别失败而循环追问。"""
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("我要 M码 深灰色", _pants_skus())
    assert len(matched) == 1
    assert matched[0]["options"] == {"尺码": "M码", "颜色": "深灰色"}


def test_letter_size_l_does_not_match_xl(store: CartStore):
    """关键边界：'L码' 不能命中 'XL码'（左边界守卫），否则 XL 句会误判含 L。"""
    tool = CartTool(cart_store=store)
    q = "我要 XL码 黑色"
    assert tool._value_match_score("XL码", q) == 1
    assert tool._value_match_score("L码", q) == 0
    matched = tool._filter_skus(q, _pants_skus())
    assert len(matched) == 1
    assert matched[0]["options"] == {"尺码": "XL码", "颜色": "黑色"}


# --------------------- 全品类精确整值匹配（前端规格卡场景）---------------------

def _beauty_skus() -> list[dict]:
    """美妆：规格容量 + 适用肤质（共享 'ml' 数字前缀的容量）。"""
    skus = []
    for spec in ("30ml", "50ml", "30ml*2"):
        for skin in ("干性肌肤", "油性肌肤", "中性肌肤"):
            skus.append({"sku_id": f"{spec}_{skin}", "options": {"规格": spec, "肤质": skin}})
    return skus


def _food_skus() -> list[dict]:
    """食品：口味 + 规格（净含量共享数字）。"""
    skus = []
    for flavor in ("原味", "藤椒味", "海苔味"):
        for size in ("100g", "100g*3", "500g"):
            skus.append({"sku_id": f"{flavor}_{size}", "options": {"口味": flavor, "规格": size}})
    return skus


def test_filter_skus_exact_match_phone_all_dims(store: CartStore):
    """手机：前端卡片发 canonical 全值，三维度精确唯一命中。"""
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("我要 12GB+256GB 青釉绿 标准版", _phone_skus())
    assert len(matched) == 1
    assert matched[0]["options"] == {"存储": "12GB+256GB", "颜色": "青釉绿", "版本": "标准版"}


def test_filter_skus_exact_match_beauty_multipack(store: CartStore):
    """美妆：'30ml*2' 不能被 '30ml' 抢（精确取最长值）。"""
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("我要 30ml*2 干性肌肤", _beauty_skus())
    assert len(matched) == 1
    assert matched[0]["options"] == {"规格": "30ml*2", "肤质": "干性肌肤"}
    # 单装也能精确命中
    single = tool._filter_skus("我要 30ml 油性肌肤", _beauty_skus())
    assert len(single) == 1
    assert single[0]["options"] == {"规格": "30ml", "肤质": "油性肌肤"}


def test_filter_skus_exact_match_food_multipack(store: CartStore):
    """食品：'100g*3' 不能被 '100g' 抢，口味+规格精确唯一命中。"""
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("我要 藤椒味 100g*3", _food_skus())
    assert len(matched) == 1
    assert matched[0]["options"] == {"口味": "藤椒味", "规格": "100g*3"}


def test_filter_skus_fuzzy_fallback_partial_color(store: CartStore):
    """打字近似说法（只说 '深灰' 不带 '色'）走模糊回退仍能命中。"""
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("M码 深灰", _pants_skus())
    assert len(matched) == 1
    assert matched[0]["options"] == {"尺码": "M码", "颜色": "深灰色"}


def test_filter_skus_partial_spec_keeps_asking(store: CartStore):
    """只说了颜色没说尺码 → 仍有多个 SKU，触发继续追问（不静默乱选）。"""
    tool = CartTool(cart_store=store)
    matched = tool._filter_skus("深灰色", _pants_skus())
    assert len(matched) == 3  # M/L/XL 三个深灰色，无法唯一



