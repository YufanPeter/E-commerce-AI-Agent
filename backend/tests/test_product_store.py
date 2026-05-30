"""ProductStore 单元测试：确定性 SQLite 事实层（无模型、无网络）。

覆盖硬匹配各维度的成功路径与边界条件，以及详情/SKU/价格区间查询。
"""

from __future__ import annotations

from store.product_store import PriceRange, ProductStore, price_display

from .conftest import CHEAPEST_NON_APPLE_LAPTOP, CLEANSER, NIKE_RUNNING_SHOE


# ---------------------------------------------------------------------------
# find_candidates：类目 / 子类目
# ---------------------------------------------------------------------------

def test_find_candidates_by_category(store: ProductStore):
    results = store.find_candidates(category="服饰运动")
    assert results, "服饰运动 应有候选商品"
    assert all(c.category == "服饰运动" for c in results)


def test_find_candidates_by_sub_category(store: ProductStore):
    results = store.find_candidates(sub_category="跑步鞋")
    assert {c.product_id for c in results} >= {NIKE_RUNNING_SHOE}
    assert all(c.sub_category == "跑步鞋" for c in results)


def test_find_candidates_unknown_category_is_empty(store: ProductStore):
    # 边界：不存在的类目返回空
    assert store.find_candidates(category="不存在的类目") == []


# ---------------------------------------------------------------------------
# find_candidates：价格区间（基于 SKU 价，而非 base_price）
# ---------------------------------------------------------------------------

def test_min_price_keeps_in_range(store: ProductStore):
    results = store.find_candidates(sub_category="跑步鞋", min_price=300)
    assert NIKE_RUNNING_SHOE in {c.product_id for c in results}


def test_max_price_excludes_when_no_sku_below(store: ProductStore):
    # 边界：笔记本最低 SKU 6299，max_price=5000 → 该子类目应为空
    assert store.find_candidates(sub_category="笔记本电脑", max_price=5000) == []


def test_price_boundary_is_inclusive_on_sku_price(store: ProductStore):
    # Nike 跑鞋最低 SKU = 899。max_price=899 命中（含等于），898 落空。
    ids_at = {c.product_id for c in store.find_candidates(sub_category="跑步鞋", max_price=899)}
    ids_below = {c.product_id for c in store.find_candidates(sub_category="跑步鞋", max_price=898)}
    assert NIKE_RUNNING_SHOE in ids_at
    assert NIKE_RUNNING_SHOE not in ids_below


# ---------------------------------------------------------------------------
# find_candidates：品牌包含 / 排除
# ---------------------------------------------------------------------------

def test_brand_include(store: ProductStore):
    results = store.find_candidates(sub_category="跑步鞋", brand_include=["耐克"])
    assert results
    assert all(c.brand == "耐克" for c in results)
    assert NIKE_RUNNING_SHOE in {c.product_id for c in results}


def test_brand_exclude(store: ProductStore):
    results = store.find_candidates(sub_category="笔记本电脑", brand_exclude=["Apple 苹果"])
    assert results
    assert all(c.brand != "Apple 苹果" for c in results)


def test_brand_include_nonexistent_is_empty(store: ProductStore):
    # 边界：指定不存在的品牌 → 空（fallback 的触发前提）
    assert store.find_candidates(sub_category="徒步鞋", brand_include=["始祖鸟"]) == []


# ---------------------------------------------------------------------------
# get_products_by_ids
# ---------------------------------------------------------------------------

def test_get_products_by_ids_preserves_order_and_skips_missing(store: ProductStore):
    requested = [NIKE_RUNNING_SHOE, "p_does_not_exist", CLEANSER]
    results = store.get_products_by_ids(requested)
    assert [c.product_id for c in results] == [NIKE_RUNNING_SHOE, CLEANSER]


def test_get_products_by_ids_empty_input(store: ProductStore):
    assert store.get_products_by_ids([]) == []


# ---------------------------------------------------------------------------
# get_product_detail / get_skus
# ---------------------------------------------------------------------------

def test_get_product_detail_success(store: ProductStore):
    detail = store.get_product_detail(NIKE_RUNNING_SHOE)
    assert detail is not None
    assert detail.product_id == NIKE_RUNNING_SHOE
    assert detail.brand == "耐克"
    assert detail.skus, "详情应包含 SKU"
    assert detail.price_range.min_price <= detail.price_range.max_price


def test_get_product_detail_not_found(store: ProductStore):
    # 边界：未知商品 → None（API 层据此返回 404）
    assert store.get_product_detail("p_not_real") is None


def test_get_skus_price_filter_boundary(store: ProductStore):
    # Nike SKU 价：标准楦 899，宽楦 949
    cheap = store.get_skus(NIKE_RUNNING_SHOE, max_price=899)
    pricey = store.get_skus(NIKE_RUNNING_SHOE, min_price=949)
    assert cheap and all(s.price <= 899 for s in cheap)
    assert pricey and all(s.price >= 949 for s in pricey)


def test_cleanser_has_distinct_sku_prices(store: ProductStore):
    # SKU 计价 bug 的数据前提：同一商品不同 SKU 价格不同
    detail = store.get_product_detail(CLEANSER)
    assert detail is not None
    prices = {s.price for s in detail.skus}
    assert len(prices) >= 2, "洁面应有多个不同价位的 SKU"


# ---------------------------------------------------------------------------
# price_display 纯函数
# ---------------------------------------------------------------------------

def test_price_display_single_price():
    assert price_display(PriceRange(min_price=52, max_price=52, sku_count=1)) == "¥52"


def test_price_display_range_uses_qi():
    assert price_display(PriceRange(min_price=52, max_price=69, sku_count=2)) == "¥52 起"
