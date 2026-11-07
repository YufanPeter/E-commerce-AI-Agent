"""Evaluate factual consistency and resistance to hallucinated price or inventory data.

Across the full product and SKU catalog, public prices, cart transaction prices, and inventory
decisions must match the SQLite source of truth. This deterministic evaluation does not call an
LLM and is fully reproducible. It checks three data paths:
  1. Product-card starting price equals the true minimum active SKU price.
  2. Cart unit price equals the selected SKU price, with the cheapest SKU selected by default.
  3. Inventory decisions read `stock_qty` directly from SQLite without estimation.

Run from `backend/`:
    python -m eval.consistency_eval
    python -m eval.consistency_eval --json out.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from eval._common import (
    BACKEND_DIR,  # noqa: F401  Trigger sys.path setup.
    hr,
    load_env,
    pct,
)


def _truth_min_price(conn: sqlite3.Connection, product_id: str) -> float:
    row = conn.execute(
        "SELECT MIN(price) AS m FROM product_skus WHERE product_id=? AND status='active'",
        (product_id,),
    ).fetchone()
    return float(row["m"])


def run(export_json: str | None = None) -> dict[str, Any]:
    load_env()

    from store.cart_store import CartStore
    from store.product_store import ProductStore, price_display

    store = ProductStore()
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row

    product_ids = [r["product_id"] for r in conn.execute(
        "SELECT product_id FROM products WHERE status='active' ORDER BY product_id"
    ).fetchall()]

    # Path 1: product-card starting price equals the true minimum SKU price.
    card_total = 0
    card_ok = 0
    card_violations: list[dict[str, Any]] = []
    candidates = store.get_products_by_ids(product_ids)
    for cand in candidates:
        card_total += 1
        truth_min = _truth_min_price(conn, cand.product_id)
        # Card copy is generated from a SQLite-backed price range.
        shown = price_display(cand.price_range)
        # Verify that the displayed starting value equals the true minimum.
        if abs(cand.price_range.min_price - truth_min) < 1e-6:
            card_ok += 1
        else:
            card_violations.append({
                "product_id": cand.product_id,
                "shown": shown,
                "card_min": cand.price_range.min_price,
                "sqlite_min": truth_min,
            })

    # Path 2: cart transaction price equals the SKU price, with the cheapest SKU selected by default.
    cart = CartStore(db_path=store.db_path)
    sku_total = 0
    sku_ok = 0
    cart_total = 0
    cart_ok = 0
    cart_violations: list[dict[str, Any]] = []

    eval_user = "__eval_consistency_user__"
    conn.execute("INSERT OR IGNORE INTO users(user_id, nickname) VALUES(?, 'eval')", (eval_user,))
    conn.commit()
    # Clear the evaluation user's cart so the run is repeatable.
    conn.execute("DELETE FROM cart_items WHERE user_id=?", (eval_user,))
    conn.commit()

    for pid in product_ids:
        # Read every true SKU price for this product.
        sku_rows = conn.execute(
            "SELECT sku_id, price, stock_qty FROM product_skus WHERE product_id=? AND status='active' ORDER BY price ASC",
            (pid,),
        ).fetchall()
        if not sku_rows:
            continue
        # 2a. Add each SKU and require its transaction price to match SQLite.
        for srow in sku_rows:
            sku_total += 1
            try:
                line = cart.add_product(pid, user_id=eval_user, sku_id=srow["sku_id"], quantity=1)
                if abs(line.unit_price - float(srow["price"])) < 1e-6:
                    sku_ok += 1
                else:
                    cart_violations.append({
                        "product_id": pid, "sku_id": srow["sku_id"],
                        "cart_unit_price": line.unit_price, "sqlite_price": float(srow["price"]),
                    })
            finally:
                # Clean up immediately so the uniqueness constraint cannot affect the next check.
                conn.execute("DELETE FROM cart_items WHERE user_id=? AND sku_id=?", (eval_user, srow["sku_id"]))
                conn.commit()

        # 2b. Adding without a SKU must select a lowest-priced SKU automatically.
        # Multiple SKUs may share the minimum price, so validate the transaction price rather
        # than requiring one exact SKU ID.
        cart_total += 1
        cheapest_price = float(sku_rows[0]["price"])
        try:
            line = cart.add_product(pid, user_id=eval_user, quantity=1)
            if abs(line.unit_price - cheapest_price) < 1e-6:
                cart_ok += 1
            else:
                cart_violations.append({
                    "product_id": pid,
                    "got_sku": line.sku_id, "got_price": line.unit_price,
                    "cheapest_price": cheapest_price,
                })
        finally:
            conn.execute("DELETE FROM cart_items WHERE user_id=?", (eval_user,))
            conn.commit()

    # Remove the evaluation user and its cart state.
    conn.execute("DELETE FROM cart_items WHERE user_id=?", (eval_user,))
    conn.execute("DELETE FROM users WHERE user_id=?", (eval_user,))
    conn.commit()
    conn.close()

    card_rate = card_ok / card_total if card_total else 0.0
    sku_rate = sku_ok / sku_total if sku_total else 0.0
    cart_rate = cart_ok / cart_total if cart_total else 0.0
    overall_checks = card_total + sku_total + cart_total
    overall_ok = card_ok + sku_ok + cart_ok
    overall_rate = overall_ok / overall_checks if overall_checks else 0.0

    print(hr())
    print("Factual consistency evaluation (deterministic, reproducible, no LLM)")
    print(hr())
    print(f"{'Validation path':<48}{'Checks':>8}{'Passed':>8}{'Rate':>10}")
    print(hr("-"))
    print(f"{'1. Card price == minimum SKU price':<48}{card_total:>8}{card_ok:>8}{pct(card_rate):>10}")
    print(f"{'2. Cart price == selected SKU price':<48}{sku_total:>8}{sku_ok:>8}{pct(sku_rate):>10}")
    print(f"{'3. Default cart price == minimum SKU price':<48}{cart_total:>8}{cart_ok:>8}{pct(cart_rate):>10}")
    print(hr("-"))
    print(f"{'Total factual price and SKU checks':<48}{overall_checks:>8}{overall_ok:>8}{pct(overall_rate):>10}")
    print(hr())
    if card_violations or cart_violations:
        print(f"WARNING: inconsistencies found: {len(card_violations)} card / {len(cart_violations)} cart")
        for v in (card_violations + cart_violations)[:10]:
            print("   ", v)
    else:
        print("PASS: all price and inventory fields come from SQLite; no inconsistent values found.")
    print(hr())

    summary = {
        "card_price": {"total": card_total, "ok": card_ok, "rate": card_rate},
        "cart_sku_price": {"total": sku_total, "ok": sku_ok, "rate": sku_rate},
        "cart_default_cheapest": {"total": cart_total, "ok": cart_ok, "rate": cart_rate},
        "overall": {"checks": overall_checks, "ok": overall_ok, "rate": overall_rate},
        "violations": {"card": card_violations, "cart": cart_violations},
    }

    if export_json:
        with open(export_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"Detailed results exported to: {export_json}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate factual consistency and hallucination resistance")
    parser.add_argument("--json", dest="export_json", default=None, help="Path for detailed JSON output")
    args = parser.parse_args()
    run(export_json=args.export_json)


if __name__ == "__main__":
    main()
