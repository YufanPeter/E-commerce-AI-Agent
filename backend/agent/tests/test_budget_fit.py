"""有预算时：卡片带价格区间与预算内规格数，排序给预算内占比加成。"""

from __future__ import annotations

from types import SimpleNamespace

from agent.tools.recommend import RecommendTool, _to_product_card
from search.query_understanding import ParsedQuery
from store.product_store import PriceRange, ProductCandidate, ProductSku


def _hit(pid, score, lo, hi):
    return SimpleNamespace(product_id=pid, title=pid, brand="", category="", sub_category="",
                           base_price=lo, score=score, min_price=lo, max_price=hi, sku_count=2)


def test_budget_fit_bonus_prefers_products_mostly_within_budget():
    # a 相关性略高但只有最低配在预算内；b 全系在预算内 → b 升到前面
    hits = [_hit("a", 0.95, 480, 1600), _hit("b", 0.90, 300, 480), _hit("c", 0.10, 100, 200)]
    out = RecommendTool._apply_budget_fit_bonus(hits, ParsedQuery(original_query="q", max_price=500))
    assert [h.product_id for h in out] == ["b", "a", "c"]
    # 没预算：原序不变
    out = RecommendTool._apply_budget_fit_bonus(hits, ParsedQuery(original_query="q"))
    assert [h.product_id for h in out] == ["a", "b", "c"]


def test_budget_fit_never_moves_unconfirmed_products_above_confirmed_ones():
    confirmed = _hit("confirmed", 0.10, 450, 500)
    confirmed.attribute_checks = {"battery_hours >= 20": "meets"}
    unknown = _hit("unknown", 0.95, 100, 200)
    unknown.attribute_checks = {"battery_hours >= 20": "unknown"}
    parsed = ParsedQuery(original_query="q", sub_category="Bluetooth Speakers", max_price=500)
    out = RecommendTool._apply_budget_fit_bonus([confirmed, unknown], parsed)
    assert [h.product_id for h in out] == ["confirmed", "unknown"]


def test_card_carries_price_range_and_budget_info():
    class _Store:
        def get_products_by_ids(self, ids):
            return [ProductCandidate(
                product_id="a", title="A", brand="", category="", sub_category=None, base_price=480,
                image_path=None, image_url=None, price_range=PriceRange(min_price=480, max_price=1600, sku_count=3),
                matched_skus=[],
            )]

        def get_skus(self, pid, max_price=None):
            return [
                ProductSku(sku_id="s1", product_id="a", properties={}, price=480, stock_qty=1, status="active"),
                ProductSku(sku_id="s2", product_id="a", properties={}, price=900, stock_qty=1, status="active"),
                ProductSku(sku_id="s3", product_id="a", properties={}, price=1600, stock_qty=1, status="active"),
            ]

    tool = RecommendTool(search_service=SimpleNamespace(), product_store=_Store())
    card = _to_product_card(_hit("a", 1.0, 480, 1600))
    assert card["price"] == 480 and card["price_range"] == {"min": 480, "max": 1600, "sku_count": 2}

    tool._attach_price_displays([card], ParsedQuery(original_query="q", max_price=500))
    assert card["price_display"] == "¥480 – ¥1600"
    assert card["price_range"] == {"min": 480, "max": 1600, "sku_count": 3}
    assert card["budget"]["skus_within_budget"] == 1
    assert card["budget"]["cheapest_sku_id"] == "s1"
    assert card["budget"]["max_price"] == 500

    plain = _to_product_card(_hit("a", 1.0, 480, 1600))
    tool._attach_price_displays([plain], ParsedQuery(original_query="q"))
    assert "budget" not in plain
