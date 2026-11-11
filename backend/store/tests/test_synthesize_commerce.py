"""Invariants of simulated Amazon commerce data and English spec matching."""

from store.sku_select import _pick_english_value
from store.synthesize_commerce import load_rules, synthesize_skus


RULES = load_rules()


def test_base_sku_keeps_source_price_and_stock():
    for label in ("Men's T-Shirts", "Micro SD Cards", "Bedding Sets", "Unmapped Label"):
        skus = synthesize_skus("B000TEST01", label, 19.99, [], RULES)
        assert min(sku["price"] for sku in skus) == 19.99
        assert skus[0]["price"] == 19.99 and skus[0]["stock_qty"] > 0
        assert all(sku["sku_id"].startswith("sim_B000TEST01_") for sku in skus)


def test_synthesis_is_deterministic_and_prefers_observed_color():
    first = synthesize_skus("B000TEST02", "Smartwatches", 50.0, ["Gold"], RULES)
    assert first == synthesize_skus("B000TEST02", "Smartwatches", 50.0, ["Gold"], RULES)
    assert first[0]["properties"] == {"Color": "Gold"}
    assert synthesize_skus("B000TEST03", "Unmapped Label", 5.0, [], RULES)[0]["properties"] == {"Option": "Standard"}


def test_english_spec_matching_uses_whole_values():
    sizes = ["XS", "S", "M", "L", "XL", "XXL"]
    assert _pick_english_value(sizes, "size M") == "M"
    assert _pick_english_value(sizes, "I'm fine with large") == "L"
    assert _pick_english_value(sizes, "it's extra large") == "XL"
    assert _pick_english_value(sizes, "the first one") is None
    assert _pick_english_value(["8", "8.5", "9"], "size 8.5") == "8.5"
    assert _pick_english_value(["8", "8.5", "9"], "size 8.") == "8"
    assert _pick_english_value(["128GB", "256GB"], "256 gb") == "256GB"
    assert _pick_english_value(["3 ft", "10 ft"], "10 feet") == "10 ft"
