from __future__ import annotations

"""Local CLI for inspecting a built Chroma collection.

This is a development utility, not a user-facing API. Long-running services should
create and reuse one ``ChromaRetriever`` instance.
"""

import argparse
import json
import time
from pathlib import Path
from typing import Any

from rag.chroma_store import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_PERSIST_DIR,
)
from rag.retriever import ChromaRetriever


def parse_where(value: str) -> dict[str, Any] | None:
    """Parse a JSON metadata filter supplied on the command line."""
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("--where must be a JSON object")
    return parsed


def preview_result(chunks: list[Any], max_chars: int) -> None:
    """Print a compact retrieved-chunk preview for manual inspection."""
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk.metadata
        distance_text = f" distance={chunk.distance:.4f}" if chunk.distance else ""
        print(
            f"{index}. {chunk.chunk_id} | {metadata.get('product_id')} | "
            f"{metadata.get('chunk_type')} | {metadata.get('title')}" + distance_text
        )
        print(f"   {chunk.document[:max_chars]}...")


def parse_args() -> argparse.Namespace:
    """Parse local retrieval-inspection arguments."""
    parser = argparse.ArgumentParser(description="Query an existing Chroma collection.")
    parser.add_argument("query")
    parser.add_argument("--persist-dir", type=Path, default=DEFAULT_PERSIST_DIR)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--where", type=parse_where, default=None)
    parser.add_argument("--preview-chars", type=int, default=120)
    return parser.parse_args()


def main() -> None:
    """Run repeated retrieval and print timings plus the leading chunks."""
    args = parse_args()

    init_start = time.perf_counter()
    retriever = ChromaRetriever(
        persist_dir=args.persist_dir,
        collection_name=args.collection,
        embedding_model=args.embedding_model,
    )
    init_seconds = time.perf_counter() - init_start
    print(f"collection={args.collection} count={retriever.count()}")
    print(f"init_seconds={init_seconds:.3f}")

    last_chunks: list[Any] = []
    for run_index in range(1, args.repeat + 1):
        query_start = time.perf_counter()
        last_chunks = retriever.search(args.query, top_k=args.top_k, where=args.where)
        query_seconds = time.perf_counter() - query_start
        print(f"query_run_{run_index}_seconds={query_seconds:.3f}")

    if last_chunks:
        preview_result(last_chunks, args.preview_chars)


if __name__ == "__main__":
    main()
