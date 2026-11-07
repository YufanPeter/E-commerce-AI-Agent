"""商品图由后端自己提供：imageURL 指向 /images/{id}，文件从 data/ 读取。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app


def test_product_image_url_points_to_backend_and_file_is_served():
    client = TestClient(app, base_url="http://testserver")
    detail = client.get("/products/p_beauty_001").json()
    assert detail["imageURL"] == "http://testserver/images/p_beauty_001"

    image = client.get("/images/p_beauty_001")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")
    assert len(image.content) > 1000

    batch = client.get("/products", params={"ids": "p_beauty_001,p_beauty_002"}).json()
    assert [p["imageURL"] for p in batch["products"]] == [
        "http://testserver/images/p_beauty_001",
        "http://testserver/images/p_beauty_002",
    ]


def test_configured_base_url_wins(monkeypatch):
    monkeypatch.setenv("PRODUCT_IMAGE_BASE_URL", "https://cdn.example.com/")
    client = TestClient(app)
    detail = client.get("/products/p_beauty_001").json()
    assert detail["imageURL"] == "https://cdn.example.com/images/p_beauty_001"


def test_unknown_image_is_404():
    client = TestClient(app)
    assert client.get("/images/nope").status_code == 404
