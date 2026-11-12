"""Every popular-search chip finds the product it was built from in the Amazon catalog.

``/suggestions`` samples its chips from brand/type pairs and bare types; this walks the
whole population it samples from, built with the endpoint's own query and wording.
Parsing runs on rules and ranking on fused order, so the check is offline and repeatable.
"""

from unittest.mock import patch

import pytest

from api.main import _HOT_PAIRS_SQL, _hot_search_term
from catalog_config import AMAZON_DB_PATH
from search import catalog_understanding
from search.catalog_search import CatalogSearchService
from store.product_store import ProductStore


pytestmark = pytest.mark.skipif(not AMAZON_DB_PATH.exists(), reason="needs the built Amazon catalog")


class _NoVectors:
    def search(self, *args, **kwargs):
        return []


class _FusedOrder:
    def rerank(self, query, chunks, top_k=None):
        return []


@pytest.fixture(scope="module")
def chips():
    store = ProductStore(AMAZON_DB_PATH)
    with store.connect() as conn:
        pairs = [
            (row["brand"], row["sub_category"])
            for row in conn.execute(_HOT_PAIRS_SQL[True]).fetchall()
            if row["brand"].isascii()
        ]
        types = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT sub_category FROM products WHERE status='active' AND sub_category <> ''"
            ).fetchall()
        ]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CARTPILOT_GRAPH", "0")
        service = CatalogSearchService(store, _NoVectors(), _NoVectors(), reranker=_FusedOrder())
    return service, pairs, types


def _misses(service, wanted):
    """Chips whose results hold no product with the brand and type the chip names."""
    missed = []
    with patch.object(catalog_understanding, "_call_model", side_effect=TimeoutError("offline")), \
            patch("socket.socket.connect", side_effect=AssertionError("unexpected outbound network")):
        for term, brand, product_type in wanted:
            hits = service.search(term).hits
            if not any(hit.sub_category == product_type and brand in (None, hit.brand) for hit in hits):
                missed.append(term)
    return missed


def test_every_brand_chip_finds_its_product(chips):
    service, pairs, _ = chips
    assert pairs
    # e.g. "FEEL2NICE Lightning Cables" (title leads with "Charger"), "HP Computer Mice"
    # (two-letter brand), "Grunt Style Novelty Men's T-Shirts" (label longer than its terms).
    assert _misses(service, [(_hot_search_term(brand, sub), brand, sub) for brand, sub in pairs]) == []


def test_every_type_chip_finds_its_type(chips):
    service, _, types = chips
    assert _misses(service, [(sub, None, sub) for sub in types]) == []
