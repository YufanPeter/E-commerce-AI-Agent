"""Offline evaluation suite for RAG retrieval, routing, consistency, and exclusion filtering.

All metrics use fixed annotated datasets so runs are reproducible and suitable for reports.

Modules:
- datasets/        : Manually annotated JSON evaluation sets
- retrieval_eval   : RAG retrieval quality, comparing reranking with vector-only retrieval
- routing_eval     : Seven-class tool-routing accuracy and confusion matrix
- consistency_eval : Price and inventory consistency against SQLite facts
- negative_eval    : Exclusion-filter success for constraints such as avoiding an ingredient
- run_all          : Aggregated entry point that runs every evaluation and prints a summary
"""
