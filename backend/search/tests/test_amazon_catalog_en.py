"""Regression coverage for the English Amazon API without external model calls."""

import json
import os
from pathlib import Path
import subprocess
import sys

from store.import_amazon import build_amazon_catalog


CELLS = {
    "Smartwatches": "Electronics > Wearable Technology > Smartwatches",
    "Smartwatch Bands": "Cell Phones & Accessories > Accessories > Smartwatch Accessories > Smartwatch Bands",
}


def test_english_amazon_catalog_turns(tmp_path):
    source = tmp_path / "source"
    data = source / "electronics" / "data"
    data.mkdir(parents=True)
    manifest = {}
    samples = [
        ("B000000001", "Atlas Smartwatch", "Atlas", 25, "Smartwatches"),
        ("B000000002", "Summit Smartwatch", "Summit", 42, "Smartwatches"),
        ("B000000003", "Apex Smartwatch", "Apex", 65, "Smartwatches"),
        ("B000000004", "Atlas Smartwatch Band", "Atlas", 8, "Smartwatch Bands"),
    ]
    for asin, title, brand, usd, cell in samples:
        pid = f"amz_{asin}"
        url = f"https://example.com/{asin}.jpg"
        manifest[pid] = url
        (data / f"{pid}.json").write_text(json.dumps({
            "product_id": pid, "title": title, "brand": brand, "category": "数码电子",
            "base_price": round(usd * 7.2, 2), "image_path": "",
            "source": {"dataset": "amazon_reviews_2023", "parent_asin": asin, "image_url": url,
                       "cell": CELLS[cell], "amazon_category": "Electronics",
                       "price_usd": usd, "average_rating": 4.5, "rating_number": 30},
            "rag_knowledge": {"marketing_description": f"Listing details for the {title}.",
                              "user_reviews": [{"nickname": "Amazon 用户", "content": "Easy to use.", "rating": 4}]},
            "skus": [{"sku_id": "placeholder", "price": usd * 7.2}],
        }), encoding="utf-8")
    (source / "image_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "sample_report.json").write_text(json.dumps({"summary": {"products": 4}, "usd_cny": 7.2}))
    db = tmp_path / "catalog.sqlite3"
    assert build_amazon_catalog(source, db) == 4
    # A fresh process ensures the process-level catalog mode matches what Uvicorn uses.
    check = r"""
import json
from unittest.mock import patch
from fastapi.testclient import TestClient
from api.main import app

c = TestClient(app)
with patch("socket.socket.connect", side_effect=AssertionError("unexpected outbound network")):
    assert c.get("/health").status_code == 200
    products = c.get("/products/search", params={"q": "Smartwatches under $50, no watch bands"}).json()["products"]
    assert {p["price"]["amountMinor"] for p in products} == {2500, 4200}
    assert all(p["price"]["currency"] == "USD" and p["category"] == "Electronics"
               and p["priceType"] == "simulated" and p["availability"] == "inStock" and p["skus"]
               and all(s["id"].startswith("sim_") for s in p["skus"])
               and p["reviews"][0]["nickname"] == "Amazon customer" for p in products)
    session = None
    def turn(q, tool):
        global session
        result = c.post("/chat", json={"query": q, "session_id": session}).raise_for_status().json()
        session = result["session_id"]
        assert result["decision"]["tool"] == tool and not result["trace"].get("tool_error"), result
        return result
    first = turn("Find smartwatches under $80, no watch bands", "recommend")
    assert len(first["tool_result"]["payload"]["products"]) == 3
    narrowed = turn("Under $50", "refine")
    ordered = [p["product_id"] for p in narrowed["tool_result"]["payload"]["products"]]
    assert len(ordered) == 2
    compared = turn("Compare the second and first", "compare")
    assert [p["product_id"] for p in compared["tool_result"]["payload"]["comparison"]["products"]] == ordered[::-1]
    detail = turn("Show reviews for the second one", "product_detail")
    assert detail["tool_result"]["payload"]["product"]["product_id"] == ordered[0]
    assert "Easy to use." in detail["narrative"]
    added = c.post("/cart/mutate", json={"action": "add", "productID": ordered[0]}).raise_for_status().json()
    assert len(added["cart"]["lines"]) == 1
    too_many = c.post("/cart/mutate", json={"action": "add", "productID": ordered[0], "quantity": 5000})
    assert too_many.status_code == 409 and too_many.json()["code"] == "OUT_OF_STOCK"
    picking = turn("Add the second one to my cart", "cart")
    assert "several options" in picking["narrative"]
    colors = picking["tool_result"]["payload"]["dimensions"][0]["values"]
    bought = turn(colors[1], "cart")
    assert "Added" in bought["narrative"] and "$" in bought["narrative"] and "元" not in bought["narrative"]
    assert "Order placed" in turn("Checkout", "cart")["narrative"]
    saved = c.post("/memory/english-test/tell", json={"text": "I avoid Atlas"}).raise_for_status().json()
    assert saved["view"]["groups"][0]["items"][0]["label"] == "Avoids Atlas"
    with c.stream("POST", "/chat/stream", json={"query": "Smartwatches", "user_id": "english-test"}) as stream:
        stream.raise_for_status()
        body = "".join(stream.iter_text())
    assert "event: narrative" in body and "Thinking" in body and "event: done" in body
    assert "amz_B000000001" not in body and "amz_B000000002" in body
"""
    backend = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(backend), "CARTPILOT_CATALOG": "amazon",
           "CARTPILOT_AMAZON_DB": str(db), "CARTPILOT_AMAZON_MODEL_COPY": "0", "CARTPILOT_AMAZON_LLM": "0",
           "CARTPILOT_AMAZON_VISION": "0", "BACKEND_WARMUP": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", check], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
