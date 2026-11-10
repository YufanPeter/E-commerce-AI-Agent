"""Process-local catalog selection; the demo and Amazon databases are separate."""

from dataclasses import dataclass
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = PROJECT_ROOT / "backend" / "storage"
AMAZON_DATA_DIR = PROJECT_ROOT / "amazon_dense_10k"
AMAZON_DB_PATH = Path(
    os.environ.get("CARTPILOT_AMAZON_DB") or STORAGE_DIR / "amazon" / "catalog_en.sqlite3"
).expanduser().resolve()
AMAZON_CATEGORIES = {
    "美妆护肤": "Beauty & Personal Care",
    "数码电子": "Electronics",
    "服饰运动": "Clothing & Sports",
    "家居厨房": "Home & Kitchen",
}


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip() == "1"


@dataclass(frozen=True)
class CatalogCapabilities:
    """Behavior a catalog supports. Business code checks these, never the catalog name."""

    cart: bool
    vision: bool
    llm_copy: bool
    llm_routing: bool
    query_decomposition: bool
    llm_target_resolution: bool
    image_similarity: bool
    vector_evidence: bool
    query_prefetch: bool
    inferred_memory: bool
    llm_memory: bool
    reference_prices: bool


@dataclass(frozen=True)
class CatalogProfile:
    """Everything that differs between catalogs, resolved once per process."""

    id: str
    db_path: Path
    locale: str
    currency: str
    search_backend: str
    requires_prebuilt_db: bool
    capabilities: CatalogCapabilities


def _demo_profile() -> CatalogProfile:
    return CatalogProfile(
        id="demo",
        db_path=STORAGE_DIR / "ecommerce_agent.sqlite3",
        locale="zh",
        currency="CNY",
        search_backend="rag",
        requires_prebuilt_db=False,
        capabilities=CatalogCapabilities(
            cart=True,
            vision=True,
            llm_copy=True,
            llm_routing=True,
            query_decomposition=True,
            llm_target_resolution=True,
            image_similarity=True,
            vector_evidence=True,
            query_prefetch=True,
            inferred_memory=True,
            llm_memory=True,
            reference_prices=False,
        ),
    )


def _amazon_profile() -> CatalogProfile:
    return CatalogProfile(
        id="amazon",
        db_path=AMAZON_DB_PATH,
        locale="en",
        currency="USD",
        search_backend="catalog",
        requires_prebuilt_db=True,
        capabilities=CatalogCapabilities(
            cart=False,
            vision=_env_flag("CARTPILOT_AMAZON_VISION"),
            llm_copy=_env_flag("CARTPILOT_AMAZON_MODEL_COPY"),
            llm_routing=False,
            query_decomposition=False,
            llm_target_resolution=False,
            image_similarity=False,
            vector_evidence=False,
            query_prefetch=False,
            inferred_memory=False,
            llm_memory=False,
            reference_prices=True,
        ),
    )


_PROFILES = {"demo": _demo_profile, "amazon": _amazon_profile}

CATALOG_MODE = os.environ.get("CARTPILOT_CATALOG", "demo").strip().lower()
if CATALOG_MODE not in _PROFILES:
    raise ValueError("CARTPILOT_CATALOG must be demo or amazon")

PROFILE: CatalogProfile = _PROFILES[CATALOG_MODE]()
DEFAULT_DB_PATH = PROFILE.db_path
