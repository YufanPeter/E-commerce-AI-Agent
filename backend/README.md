# Backend RAG Data Layer

This backend folder contains the local RAG data layer for the shopping agent.

## Setup

Install dependencies:

```bash
# Run from the repository root.
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
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
