"""Process-local catalog selection; the demo and Amazon databases are separate."""

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = PROJECT_ROOT / "backend" / "storage"
AMAZON_DATA_DIR = PROJECT_ROOT / "amazon_dense_10k"
AMAZON_DB_PATH = Path(
    os.environ.get("CARTPILOT_AMAZON_DB") or STORAGE_DIR / "amazon" / "catalog_en.sqlite3"
).expanduser().resolve()
CATALOG_MODE = os.environ.get("CARTPILOT_CATALOG", "demo").strip().lower()
if CATALOG_MODE not in {"demo", "amazon"}:
    raise ValueError("CARTPILOT_CATALOG must be demo or amazon")

DEFAULT_DB_PATH = (
    AMAZON_DB_PATH if CATALOG_MODE == "amazon" else STORAGE_DIR / "ecommerce_agent.sqlite3"
)

AMAZON_MODE = CATALOG_MODE == "amazon"
AMAZON_VISION = os.environ.get("CARTPILOT_AMAZON_VISION", "0").strip() == "1"
AMAZON_MODEL_COPY = os.environ.get("CARTPILOT_AMAZON_MODEL_COPY", "0").strip() == "1"
AMAZON_CATEGORIES = {
    "美妆护肤": "Beauty & Personal Care",
    "数码电子": "Electronics",
    "服饰运动": "Clothing & Sports",
    "家居厨房": "Home & Kitchen",
}
