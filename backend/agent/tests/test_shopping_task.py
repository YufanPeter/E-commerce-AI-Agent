"""Multi-turn state: what a new list invalidates, condition edits and field-level spec changes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import turn_args
from agent.intent_router import IntentDecision, english_args
from agent.session import AgentSession, ShoppingTask
from agent.tools import cart as cart_module
from agent.tools import reference
from agent.tools.cart import CartTool
from agent.tools.reference import resolve_by_name, resolve_indices
from agent.tools.refine import RefineTool
from search.catalog_understanding import merge_turn
from search.query_understanding import ParsedQuery
from store import sku_select
from store.cart_store import CartStore

_SCHEMA = """
CREATE TABLE products (product_id TEXT PRIMARY KEY, title TEXT, brand TEXT, category TEXT,
    sub_category TEXT, base_price REAL, status TEXT DEFAULT 'active');
CREATE TABLE product_skus (sku_id TEXT PRIMARY KEY, product_id TEXT, properties_json TEXT DEFAULT '{}',
    price REAL, stock_qty INTEGER DEFAULT 99, status TEXT DEFAULT 'active');
CREATE TABLE cart_items (cart_item_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, product_id TEXT,
    sku_id TEXT, selected_options_json TEXT DEFAULT '{}', quantity INTEGER, unit_price REAL,
    selected INTEGER DEFAULT 1, UNIQUE(user_id, sku_id));
"""
# SKU ids sort differently from the sizes, so size order cannot come from the list order.
_SHIRT = [
    ("a_black_l", {"Size": "L", "Color": "Black"}, 99),
    ("b_black_m", {"Size": "M", "Color": "Black"}, 99),
    ("c_black_s", {"Size": "S", "Color": "Black"}, 99),
    ("d_white_l", {"Size": "L", "Color": "White"}, 0),
    ("e_white_m", {"Size": "M", "Color": "White"}, 99),
]
_HITS = [{"product_id": "shirt", "title": "Cotton Shirt"}, {"product_id": "mug", "title": "Ceramic Mug"}]


def _products() -> SimpleNamespace:
    return SimpleNamespace(
        match_brands_in_text=lambda text: [], search_by_keyword=lambda keyword: [], get_product_detail=lambda product_id: None,
    )


@pytest.fixture()
def store(tmp_path: Path) -> CartStore:
    path = tmp_path / "cart.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO products(product_id,title,brand,category,sub_category,base_price) VALUES (?,?,?,?,?,?)",
        [("shirt", "Cotton Shirt", "B", "C", "S", 20), ("mug", "Ceramic Mug", "B", "C", "M", 9)],
    )
    conn.executemany(
        "INSERT INTO product_skus(sku_id,product_id,properties_json,price,stock_qty) VALUES (?,?,?,?,?)",
        [(sku, "shirt", json.dumps(options), 20, stock) for sku, options, stock in _SHIRT] + [("mug1", "mug", "{}", 9, 99)],
    )
    conn.commit()
    conn.close()
    return CartStore(path)


@pytest.fixture()
def tool(store: CartStore) -> CartTool:
    return CartTool(cart_store=store, product_store=_products())


@pytest.fixture()
def english_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (reference, cart_module, sku_select):
        monkeypatch.setattr(module, "is_english", lambda: True)


@pytest.fixture()
def chinese_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (reference, cart_module, sku_select):
        monkeypatch.setattr(module, "is_english", lambda: False)


def _session(*history: str) -> AgentSession:
    session = AgentSession()
    session.remember_search({"sub_category": "Shirts"}, list(_HITS))
    for text in history:
        session.add_user(text)
    return session


def _say(tool: CartTool, session: AgentSession, text: str):
    session.add_user(text)
    return tool.run(text, session, turn_args.tool_slots("cart", text, session))


def _routed(tool: CartTool, session: AgentSession, text: str, **args):
    """What an English turn sends the cart: the router's reading of ``text``."""
    session.add_user(text)
    decision = IntentDecision("cart", text, "high", "", english_args({"cart_action": "answer", **args}))
    return tool.run(text, session, turn_args.routed_slots(decision, session))


def _stub_action(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    decision = SimpleNamespace(action=action, args={})
    monkeypatch.setattr(cart_module, "dispatch_action", lambda *args, **kwargs: decision)


# ------------------------------- Task state -------------------------------


def test_a_new_list_drops_what_referred_to_the_old_one():
    session = _session()
    task = session.task
    task.focus_on("shirt")
    task.comparison = {"product_ids": ["shirt", "mug"]}
    task.ask("cart_pick", {"candidates": []})
    task.last_added = {"product_id": "shirt", "sku_id": "b_black_m"}
    session.remember_search({"sub_category": "Earbuds"}, [{"product_id": "buds", "title": "Buds"}])
    assert (task.focus_product_id, task.comparison, task.pending, task.last_added) == (None, None, None, None)
    assert task.active_view.version == 2


def test_focus_survives_a_list_that_still_contains_it():
    session = _session()
    session.task.focus_on("mug")
    session.remember_search({}, [{"product_id": "mug", "title": "Ceramic Mug"}])
    assert session.task.focus_product_id == "mug"


def test_viewing_the_cart_keeps_the_shopping_list_for_adds():
    session = _session()
    session.task.focus_on("shirt")
    session.task.show("cart", [{"product_id": "mug", "title": "Ceramic Mug"}])
    session.task.show("cart", [{"product_id": "mug", "title": "Ceramic Mug"}])
    assert session.recall_hits() == [{"product_id": "mug", "title": "Ceramic Mug"}]
    assert session.task.add_targets() == _HITS
    assert session.task.focus_product_id == "shirt"


def test_task_round_trips_and_ignores_broken_records():
    session = _session()
    session.task.ask("cart_sku", {"product_id": "shirt", "selected": {"Size": "M"}})
    restored = AgentSession(working_memory=json.loads(json.dumps(session.persisted_memory())))
    assert restored.task == session.task
    assert ShoppingTask.from_dict({"active_view": {"unknown": 1}}) == ShoppingTask()
    assert ShoppingTask.from_dict("old") == ShoppingTask()


def test_add_it_after_a_new_search_never_uses_the_old_focus(chinese_rules, tool: CartTool, monkeypatch: pytest.MonkeyPatch):
    _stub_action(monkeypatch, "add")
    session = _session()
    session.task.focus_on("old_product")
    session.remember_search({}, list(_HITS))
    result = _say(tool, session, "把它加进来")
    assert result.payload["action"] == "ask_which_product"
    assert tool._store.list_items() == []


# ------------------------------- References -------------------------------


@pytest.mark.parametrize("query", ["Mate 6 怎么样", "5G 手机", "来 2 件", "iPhone 6s 好吗", "Pixel 7"])
def test_digits_in_model_names_or_quantities_are_not_positions(chinese_rules, query):
    assert resolve_indices(query, 9) == []


@pytest.mark.parametrize("query, indices", [("对比 1 和 3", [0, 2]), ("选 2", [1]), ("第二个", [1])])
def test_real_positions_still_resolve(chinese_rules, query, indices):
    assert resolve_indices(query, 5) == indices


@pytest.mark.parametrize("query, indices", [
    ("Compare the first and second", [0, 1]),
    ("the last one", [4]),
    ("last", [4]),
    ("add option 3", [2]),
    ("first aid kit for travel", []),
    ("Will it last longer?", []),
    ("give me a second", []),
    ("Do you have the Galaxy Buds 2?", []),
    ("second-hand watch", []),
])
def test_english_positions(english_rules, query, indices):
    assert resolve_indices(query, 5) == indices


def test_english_names_need_whole_distinctive_words(english_rules):
    hits = [
        {"title": "Air Fryer for Home", "brand": "Cosori"},
        {"title": "Pro Fryer for Home", "brand": "Ninja"},
    ]
    assert resolve_by_name("add it to my cart for me", hits) == []
    assert resolve_by_name("a pair of those", hits) == []
    assert resolve_by_name("the Ninja one", hits) == [1]
    assert resolve_by_name("the fryer", hits) == []


def test_router_arguments_reach_the_tool(english_rules, tool: CartTool):
    session = _session()
    decision = IntentDecision("cart", "", "high", "", english_args({"cart_action": "add", "positions": [2, 9], "quantity": 2}))
    slots = turn_args.routed_slots(decision, session)
    assert slots["target"]["indices"] == [1]  # Position 9 is not on screen.
    assert slots["quantity"] == 2
    session.add_user("add 2 of the second one")
    tool.run("add the first one", session, slots)
    assert [(line.product_id, line.quantity) for line in tool._store.list_items()] == [("mug", 2)]


# ------------------------------- Conditions -------------------------------


def _parsed(query: str, **fields) -> ParsedQuery:
    return ParsedQuery(original_query=query, retrieval_query=query, **fields)


def test_budget_edits_do_not_pile_up_in_the_search_text():
    current = _parsed("smartwatches", sub_category="Smartwatches")
    for text, cap in (("Under $80", 80.0), ("Under $50", 50.0), ("with GPS", None), ("Under $40", 40.0)):
        current = merge_turn(_parsed(text, max_price=cap), current, {})
    assert current.max_price == 40.0
    assert current.retrieval_query == "with GPS smartwatches"


def test_category_switch_clears_what_no_longer_applies():
    base = _parsed("smartwatches no Sony", sub_category="Smartwatches", max_price=80.0, brand_exclude=["Sony"], excluded_attributes=["alcohol"])
    merged = merge_turn(_parsed("coffee machine", sub_category="Coffee Machines"), base, {"brand_exclude": ["Acme"]})
    assert merged.max_price is None
    assert merged.brand_exclude == ["Acme"]
    assert merged.excluded_attributes == []
    assert merged.retrieval_query == "coffee machine"


def test_saved_budget_is_not_a_search_bound():
    profile = {"budget_min": 10, "budget_max": 30}
    first = merge_turn(_parsed("headphones", sub_category="Headphones"), None, profile)
    assert (first.min_price, first.max_price) == (None, None)
    base = _parsed("smartwatches", sub_category="Smartwatches", max_price=80.0)
    switched = merge_turn(_parsed("headphones", sub_category="Headphones"), base, profile)
    assert switched.max_price is None


def test_same_category_keeps_conditions():
    base = _parsed("headphones", sub_category="Headphones", max_price=80.0, brand_exclude=["Sony"])
    merged = merge_turn(_parsed("with noise cancelling"), base, {})
    assert (merged.sub_category, merged.max_price, merged.brand_exclude) == ("Headphones", 80.0, ["Sony"])


def test_withdrawn_conditions_do_not_come_back_in_the_merge():
    base = _parsed("headphones", sub_category="Headphones", brand_exclude=["Sony"], max_price=80.0)
    merged = merge_turn(_parsed("Any brand is fine"), base.without(["brand"]), {"brand_exclude": ["Acme"]})
    assert merged.brand_exclude == [] and merged.max_price == 80.0
    assert merge_turn(_parsed("no budget"), base.without(["budget"]), {"budget_max": 50}).max_price is None


def test_withdrawing_one_rejected_attribute_keeps_the_others():
    base = _parsed("toner without alcohol or fragrance", sub_category="Toners", excluded_attributes=["alcohol", "fragrance"])
    merged = merge_turn(_parsed("actually alcohol is fine"), base, {})
    assert merged.excluded_attributes == ["fragrance"]
    # "I don't need fragrance" rejects fragrance; it does not take the rejection back.
    assert merge_turn(_parsed("I don't need fragrance"), base, {}).excluded_attributes == ["alcohol", "fragrance"]


def test_measured_limits_can_be_withdrawn_or_replaced():
    limit = {"attribute": "battery_hours", "op": ">=", "value": 20.0}
    base = _parsed("speakers with 20 hours of battery", sub_category="Bluetooth Speakers", numeric=[limit])
    assert merge_turn(_parsed("battery life doesn't matter"), base, {}).numeric == []
    shorter = {"attribute": "battery_hours", "op": "<=", "value": 10.0}
    assert merge_turn(_parsed("at most 10 hours", numeric=[shorter]), base, {}).numeric == [shorter]
    ceiling = {"attribute": "battery_hours", "op": "<=", "value": 40.0}
    assert merge_turn(_parsed("at most 40 hours", numeric=[ceiling]), base, {}).numeric == [limit, ceiling]


def test_a_new_device_replaces_the_old_one():
    base = _parsed("case for iPhone 14", sub_category="Phone Cases", compatible_with=["iphone_14", "iphone"])
    merged = merge_turn(_parsed("for the iPhone 15 instead", compatible_with=["iphone_15", "iphone"]), base, {})
    assert merged.compatible_with == ["iphone_15", "iphone"]
    assert merge_turn(_parsed("in black"), base, {}).compatible_with == ["iphone_14", "iphone"]
    assert base.without(["device"]).compatible_with == []


def test_wanted_features_carry_over_until_dropped():
    base = _parsed("waterproof speakers", sub_category="Bluetooth Speakers", features=["waterproof"])
    assert merge_turn(_parsed("in black", features=["black"]), base, {}).features == ["waterproof", "black"]
    assert merge_turn(_parsed("it doesn't need to be waterproof"), base, {}).features == []
    assert base.without(["features"]).features == []


def test_naming_one_rejection_does_not_clear_the_whole_group():
    from search.catalog_understanding import narrow_clears
    base = _parsed("speakers without BPA or latex", excluded_attributes=["bpa", "latex"])
    assert narrow_clears("actually latex is fine", base, ["exclusions"]) == []
    assert narrow_clears("drop all the ingredient restrictions", base, ["exclusions", "budget"]) == ["exclusions", "budget"]
    assert narrow_clears("latex and bpa are fine", base, ["exclusions"]) == ["exclusions"]


def test_router_cannot_clear_groups_the_message_never_mentions():
    from search.catalog_understanding import names_withdrawal, narrow_clears
    every = ["budget", "brand", "measurements", "exclusions", "device", "features"]
    limit = {"attribute": "battery_hours", "op": ">=", "value": 20.0}
    base = _parsed("Sony speakers under $80", max_price=80.0, brand_include=["Sony"], numeric=[limit], excluded_attributes=["bpa"])
    # The router listed every group for a message about the battery alone.
    assert narrow_clears("battery life doesn't matter", base, every) == ["measurements"]
    assert narrow_clears("any brand is fine", base, every) == ["brand"]
    assert narrow_clears("budget doesn't matter", base, every) == ["budget"]
    assert narrow_clears("forget all the filters", base, every) == every
    assert narrow_clears("in black", base, every) == []
    assert names_withdrawal("battery life doesn't matter", base) and not names_withdrawal("in black", base)


def test_withdrawing_a_named_condition_reaches_the_tool_even_without_a_router_clear():
    session = AgentSession()
    session.remember_search(_parsed("face wash", sub_category="Face Washes", excluded_attributes=["alcohol", "fragrance"]).to_dict(), [])
    decision = IntentDecision("refine", "face wash without fragrance", "high", "", english_args({}))
    slots = turn_args.routed_slots(decision, session, "actually alcohol is fine")
    assert slots["constraint_ops"] == {"clear": []}  # Present, so the tool reads the raw message.
    assert "constraint_ops" not in turn_args.routed_slots(decision, session, "in a bigger bottle")


def test_clear_groups_cover_every_carried_condition():
    base = _parsed(
        "q", numeric=[{"attribute": "spf", "op": ">=", "value": 30.0}], excluded_attributes=["alcohol"],
        compatible_with=["iphone"], features=["vegan"],
    )
    cleared = base.without(["measurements", "exclusions", "device", "features"])
    assert (cleared.numeric, cleared.excluded_attributes, cleared.compatible_with, cleared.features) == ([], [], [], [])
    assert english_args({"clear": ["measurements", "device", "color"]})["clear"] == ["measurements", "device"]


def test_merge_base_clears_only_what_was_asked():
    base = _parsed("跑鞋", sub_category="跑鞋", max_price=500.0, brand_include=["Nike"], brand_exclude=["李宁"])
    cleared = _parsed("品牌不限").merge_base(base.without(["brand"]))
    assert (cleared.max_price, cleared.brand_include, cleared.brand_exclude) == (500.0, [], [])
    assert _parsed("预算不限").merge_base(base.without(["budget"])).max_price is None
    assert _parsed("再看看").merge_base(base).brand_include == ["Nike"]


def test_merge_base_starts_over_on_a_new_category():
    base = _parsed("跑鞋", sub_category="跑鞋", max_price=500.0, brand_exclude=["李宁"])
    merged = _parsed("耳机", sub_category="耳机").merge_base(base)
    assert (merged.sub_category, merged.max_price, merged.brand_exclude) == ("耳机", None, [])


def test_refine_applies_the_clear_operation():
    class Recommend:
        def run(self, query, session, slots):
            self.base = slots["base_parsed"]
            return SimpleNamespace(payload={})

    recommend = Recommend()
    session = AgentSession()
    session.remember_search(_parsed("headphones", brand_exclude=["Sony"], max_price=80.0).to_dict(), list(_HITS))
    RefineTool(recommend).run("Any brand is fine", session, {"constraint_ops": {"clear": ["brand"]}})
    assert recommend.base.brand_exclude == [] and recommend.base.max_price == 80.0


# ------------------------------- Spec selection -------------------------------


def _begin(tool: CartTool, session: AgentSession, text: str = "add the first one"):
    session.add_user(text)
    return tool._begin_add("shirt", 2, session, text)


def test_each_answer_sets_only_its_own_field(english_rules, tool: CartTool):
    session = _session()
    assert _begin(tool, session).payload["action"] == "ask_spec"
    assert _routed(tool, session, "medium").payload["action"] == "ask_spec"
    assert session.task.pending_data("cart_sku")["selected"] == {"Size": "M"}
    result = _routed(tool, session, "actually large, black")
    assert result.payload["added"]["sku_id"] == "a_black_l"
    assert session.task.pending is None
    assert session.task.last_added == {"product_id": "shirt", "sku_id": "a_black_l"}


def test_size_step_applies_to_an_unfinished_choice(english_rules, tool: CartTool):
    session = _session()
    _begin(tool, session)
    _routed(tool, session, "medium")
    _routed(tool, session, "one size up", size_step=1)
    assert session.task.pending_data("cart_sku")["selected"] == {"Size": "L"}


@pytest.mark.parametrize("text, cart_action", [("Checkout", "checkout"), ("Show my cart", "view")])
def test_moving_on_closes_the_question(english_rules, tool: CartTool, text, cart_action):
    session = _session()
    _begin(tool, session)
    assert _routed(tool, session, text, cart_action=cart_action).payload["action"] == "noop"
    assert session.task.pending is None


def test_a_failed_write_keeps_the_question_open(english_rules, tool: CartTool):
    session = _session()
    _begin(tool, session)
    result = _routed(tool, session, "white large")
    assert result.payload["action"] == "noop"
    assert session.task.pending_data("cart_sku") is not None
    assert tool._store.list_items() == []
    assert _routed(tool, session, "medium").payload["added"]["sku_id"] == "e_white_m"
    assert session.task.pending is None


def test_quantity_survives_the_product_question(english_rules, tool: CartTool, monkeypatch):
    monkeypatch.setattr(cart_module, "resolve_one", lambda query, candidates, **kwargs: 1)
    session = _session()
    tool._ask_which_product(_HITS, session, 3)
    _routed(tool, session, "the mug")
    assert tool._store.list_items()[0].quantity == 3


# ------------------------------- Spec change -------------------------------


def _added(tool: CartTool, sku: str = "b_black_m", quantity: int = 2) -> AgentSession:
    session = _session()
    tool._commit_add("shirt", sku, quantity, session)
    return session


def test_change_only_the_named_field(english_rules, tool: CartTool):
    session = _added(tool)
    result = _routed(tool, session, "Change it to large", cart_action="change_spec")
    assert result.payload["changed"]["sku_id"] == "a_black_l"
    assert result.payload["changed"]["quantity"] == 2
    assert [line.sku_id for line in tool._store.list_items()] == ["a_black_l"]


@pytest.mark.parametrize("text, step, sku", [
    ("same color, one size up", 1, "a_black_l"),
    ("one size down", -1, "c_black_s"),
    ("make it a bigger size", 1, "a_black_l"),
])
def test_relative_size_changes_follow_real_size_order(english_rules, tool: CartTool, text, step, sku):
    result = _routed(tool, _added(tool), text, cart_action="change_spec", size_step=step)
    assert result.payload["changed"]["sku_id"] == sku


def test_no_size_beyond_the_range(english_rules, tool: CartTool):
    session = _added(tool, "a_black_l")
    assert _routed(tool, session, "one size up", cart_action="change_spec", size_step=1).payload["action"] == "noop"
    assert [line.sku_id for line in tool._store.list_items()] == ["a_black_l"]


def test_changing_to_a_sku_already_in_the_cart_merges_lines(english_rules, tool: CartTool):
    session = _added(tool, "a_black_l", 1)
    tool._commit_add("shirt", "b_black_m", 2, session)
    _routed(tool, session, "Change it to large", cart_action="change_spec")
    assert [(line.sku_id, line.quantity) for line in tool._store.list_items()] == [("a_black_l", 3)]


def test_sold_out_change_leaves_the_cart_alone(english_rules, tool: CartTool):
    session = _added(tool, "e_white_m", 1)
    result = _routed(tool, session, "Change it to large", cart_action="change_spec")
    assert result.payload["action"] == "noop"
    assert [line.sku_id for line in tool._store.list_items()] == ["e_white_m"]


def test_change_to_a_number_without_that_size_sets_quantity(english_rules, tool: CartTool):
    _routed(tool, _added(tool), "change it to 3", cart_action="change_spec", quantity=3)
    assert tool._store.list_items()[0].quantity == 3


def test_chinese_size_step(chinese_rules, tool: CartTool):
    assert _say(tool, _added(tool), "大一码").payload["changed"]["sku_id"] == "a_black_l"
