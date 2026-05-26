# Backend Data Layer

This backend folder contains the local product store and RAG data layer for the shopping agent.

## Setup

Install dependencies:

```bash
# Run from the repository root.
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

## SQLite Product Store

Build or rebuild the local SQLite product store after product data changes:

```bash
cd backend
source ../.venv/bin/activate
python -m store.import_product_data --reset
python -m store.import_image_manifest
```

Generated SQLite files are written to `backend/storage/ecommerce_agent.sqlite3` and should not be committed.

SQLite stores deterministic product facts and detail-page content:

- `products`: product-level facts such as title, brand, category, base price, and image paths.
- `product_skus`: SKU properties and SKU prices, the source of truth for budget filtering and cart pricing.
- `product_descriptions`: marketing descriptions for detail pages and RAG evidence alignment.
- `product_faqs`: one official FAQ row per source item, aligned with Chroma FAQ chunks by `product_id + source_index`.
- `product_reviews`: one user review row per source item, aligned with Chroma review chunks by `product_id + source_index`.
- `users` and `cart_items`: demo user and cart data.
- `product_price_ranges`: a SQLite view for card price display and budget filtering by minimum SKU price.

Inspect the product store locally:

```bash
python -m store.product_store candidates --category 服饰运动 --max-price 500 --limit 5
python -m store.product_store detail p_beauty_010
python -m store.product_store reviews p_beauty_010 --polarity negative
```

Runtime code should use `ProductStore` as the SQLite fact layer:

```python
from store.product_store import ProductStore

store = ProductStore()
candidates = store.find_candidates(category="服饰运动", max_price=500)
detail = store.get_product_detail("p_beauty_010")
```

## Offline Index Build

Build or rebuild the local Chroma collection after product data changes:

```bash
cd backend
source ../.venv/bin/activate
python -m rag.build_chroma --reset
```

Generated files are written to `backend/storage/chroma/` and should not be committed.

The Chroma collection stores semantic evidence chunks only. Product prices, SKU fields, and image paths are kept in metadata for filtering/debugging, but the final source of truth should be a structured Product Store such as SQLite.

## Runtime Retrieval

User-facing APIs should create one long-running retriever instance at backend startup and reuse it for every request:

```python
from rag.retriever import ChromaRetriever

retriever = ChromaRetriever()
hits = retriever.search("适合油皮的洗面奶", top_k=5, where={"category": "美妆护肤"})
```

The runtime path should only embed the current user query and search the existing Chroma collection. It should not rebuild the index during a user request.

## Local Inspection

For development checks, query an existing Chroma collection without rebuilding it:

```bash
python -m rag.query_chroma "适合油皮的洗面奶" --repeat 3
python -m rag.query_chroma "适合油皮的洗面奶" --where '{"category":"美妆护肤"}'
```

## Performance Notes

`build_chroma` is an offline indexing command. It loads the embedding model and embeds every chunk, so it should not run during a user request.

For product search APIs, keep a backend process alive and load the Chroma collection plus embedding model once at startup. The first query in a new process includes model cold-start cost; later queries reuse the loaded model and should be much faster.

Local measurement on the current dataset:

```text
collection count: 1092 chunks
process/model init: ~64.6s
first query after init: ~0.16s
warm query in same process: ~0.01s
```

User-facing APIs should therefore reuse a long-running `ChromaRetriever` instead of spawning `python -m rag.query_chroma` per request.
