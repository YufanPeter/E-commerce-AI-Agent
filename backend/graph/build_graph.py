"""Load validated claims and the catalog structure into Neo4j.

Neo4j holds knowledge only: prices, SKUs and stock stay in SQLite. Stored claims are
validated again here, so a tightened rule applies without paying for extraction twice.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Sequence

import numpy as np

from catalog_config import AMAZON_DB_PATH, AMAZON_INDEX_DIR, AMAZON_PRODUCT_COLLECTION
from graph.extract import (
    RELATIONS, Product, cache_key, load_cache, load_ontology, load_products, validate_claims,
)
from store.attribute_rules import DESCRIPTION, TITLE, _DEVICES, extract_multi
from store.attribute_verification import YES, AttributeVerifier, Candidate, candidate, spending_guard


TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "db" / "amazon_taxonomy.json"
BUILD_REPORT_PATH = AMAZON_DB_PATH.parent / "build_report.json"
SIMILAR_NEIGHBORS = 10
LLM_PROVENANCE = "llm"
_BATCH = 2000
_LABELS = {
    "material": "Material", "ingredient": "Ingredient", "feature": "Attribute", "device": "Device",
    "use_case": "UseCase", "audience": "Audience", "aspect": "Aspect",
}
_WRITEBACK = {
    "MADE_OF": "material", "CONTAINS": "contains", "FREE_OF": "free_of",
    "HAS_ATTRIBUTE": "feature", "COMPATIBLE_WITH": "compatible_device",
}
_DESCRIPTION_FIELD = "rag_knowledge.marketing_description"
_EVIDENCE_FIELDS = {"title": "title", "description": _DESCRIPTION_FIELD}
_SOURCE_OF_FIELD = {"title": "title", _DESCRIPTION_FIELD: "description"}
# Model rows that audited below the 95% needed to state a fact to shoppers. They are kept
# as evidence, but only 'observed' rows reach filters, detail pages and comparison tables.
_RANK_ONLY_ATTRIBUTES = frozenset({"free_of"})
_HOST_CUE = re.compile(r"\b(?:for|with|fits?|supports?|compatible|replacement)\b[\s\w/&,+-]*$", re.I)


@dataclass(frozen=True)
class CatalogRow:
    product_id: str
    title: str
    brand: str
    category: str
    label: str
    group: str | None
    role: str
    generic_brand: bool
    dup_key: str | None


def own_device(title: str) -> str | None:
    """The device a listing is, as opposed to one it works with ("Watch for iPhone")."""
    found = extract_multi({TITLE: title, DESCRIPTION: ""}, _DEVICES)
    if not found:
        return None
    first = min(found, key=lambda item: item["evidence"]["start"])
    return None if _HOST_CUE.search(title[:first["evidence"]["start"]]) else str(first["value"])


def entity_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def load_catalog_rows(db_path: Path = AMAZON_DB_PATH) -> list[CatalogRow]:
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        return [CatalogRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]), r[8]) for r in conn.execute(
            """SELECT p.product_id, p.title, p.brand, p.category, p.sub_category, a.catalog_group,
                      a.product_role, a.is_generic_brand, a.dup_key
               FROM products p JOIN amazon_product_metadata a USING (product_id)
               WHERE p.status = 'active' ORDER BY p.product_id"""
        )]


def valid_claims(
    records: dict[str, dict[str, Any]], products: Sequence[Product], ontology: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate raw model output against the current rules; stale cache entries are skipped."""
    rows: list[dict[str, Any]] = []
    for product in products:
        record = records.get(cache_key(product, ontology))
        if record is None:
            continue
        claims, _ = validate_claims(record["raw"], product, ontology)
        for claim in claims:
            rows.append({
                "product_id": product.product_id, "relation": claim["relation"], "value": claim["value"],
                "source": claim["source"], "quote": claim["quote"],
                "soft": claim["source"] not in _EVIDENCE_FIELDS, "rating": product.rating_of(claim["source"]),
            })
    return rows


def rule_device_claims(db_path: Path, products: dict[str, Product], taken: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Rule-extracted compatibility is free evidence the model may have skipped."""
    rows: list[dict[str, Any]] = []
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        found = conn.execute(
            "SELECT product_id, value, evidence_field, evidence_start, evidence_end FROM product_attributes "
            "WHERE attribute = 'compatible_device' AND status = 'observed' AND provenance = 'rule'"
        ).fetchall()
    for product_id, value, field, start, end in found:
        product, source = products.get(product_id), _SOURCE_OF_FIELD.get(field)
        if product is None or source is None or (product_id, value) in taken:
            continue
        taken.add((product_id, value))
        rows.append({
            "product_id": product_id, "relation": "COMPATIBLE_WITH", "value": value, "source": source,
            "quote": (product.text_of(source) or "")[start:end], "soft": False, "rating": None,
        })
    return rows


def similar_pairs(
    ids: Sequence[str], vectors: np.ndarray, groups: dict[str, str], dup_keys: dict[str, str | None],
    neighbors: int = SIMILAR_NEIGHBORS,
) -> list[dict[str, Any]]:
    """Nearest neighbors within a product group, leaving out duplicate listings."""
    members_of: dict[str, list[int]] = defaultdict(list)
    for position, product_id in enumerate(ids):
        if product_id in groups:
            members_of[groups[product_id]].append(position)
    norms = np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    unit = vectors / norms
    pairs: list[dict[str, Any]] = []
    for members in members_of.values():
        if len(members) < 2:
            continue
        scores = unit[members] @ unit[members].T
        np.fill_diagonal(scores, -np.inf)
        duplicates: dict[str, list[int]] = defaultdict(list)
        for row, position in enumerate(members):
            if dup_keys.get(ids[position]):
                duplicates[dup_keys[ids[position]]].append(row)
        for rows in duplicates.values():
            scores[np.ix_(rows, rows)] = -np.inf
        count = min(neighbors, len(members) - 1)
        nearest = np.argpartition(-scores, count - 1, axis=1)[:, :count]
        for row, columns in enumerate(nearest):
            for column in columns:
                if np.isfinite(scores[row, column]):
                    pairs.append({
                        "a": ids[members[row]], "b": ids[members[int(column)]],
                        "score": round(float(scores[row, column]), 4),
                    })
    return pairs


def _quote_span(claim: dict[str, Any], products: dict[str, Product]) -> tuple[int, int] | None:
    text = products[claim["product_id"]].text_of(claim["source"]) or ""
    match = re.search(r"\s+".join(re.escape(word) for word in claim["quote"].split()), text, re.I)
    return (match.start(), match.end()) if match else None


def _is_listing_fact(claim: dict[str, Any]) -> bool:
    return claim["relation"] in _WRITEBACK and claim["source"] in _EVIDENCE_FIELDS


def _claim_candidate(claim: dict[str, Any], products: dict[str, Product]) -> Candidate:
    product, span = products[claim["product_id"]], _quote_span(claim, products)
    attribute = _WRITEBACK[claim["relation"]]
    if span is None:
        return candidate(attribute, claim["value"], product.label, product.title, claim["quote"], 0, len(claim["quote"]))
    return candidate(attribute, claim["value"], product.label, product.title, product.text_of(claim["source"]) or "", *span)


def verified_claims(
    claims: Sequence[dict[str, Any]], products: dict[str, Product], verifier: AttributeVerifier,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split claims into those that may be published and those the check refused.

    Only listing facts are checked; review claims stay soft signals. Refused claims carry
    the verdict, so they can be kept for audits.
    """
    facts = {index: _claim_candidate(claim, products) for index, claim in enumerate(claims) if _is_listing_fact(claim)}
    verdicts = verifier.verdicts(list(facts.values()))
    kept, refused = [], []
    for index, claim in enumerate(claims):
        verdict = verdicts[facts[index]] if index in facts else YES
        if verdict == YES:
            kept.append(claim)
        else:
            refused.append({**claim, "verdict": verdict})
    return kept, refused


def attribute_rows(claims: Iterable[dict[str, Any]], products: dict[str, Product]) -> list[tuple[Any, ...]]:
    """Listing-sourced claims in ``product_attributes`` form, located by their exact quote."""
    rows: list[tuple[Any, ...]] = []
    for claim in claims:
        if not _is_listing_fact(claim):
            continue
        span = _quote_span(claim, products)
        if span:
            attribute = _WRITEBACK[claim["relation"]]
            status = "ambiguous" if attribute in _RANK_ONLY_ATTRIBUTES else "observed"
            rows.append((claim["product_id"], attribute, claim["value"], status,
                         _EVIDENCE_FIELDS[claim["source"]], *span, LLM_PROVENANCE))
    return rows


def rejection_rows(refused: Iterable[dict[str, Any]], products: dict[str, Product]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for claim in refused:
        start, end = _quote_span(claim, products) or (0, 0)
        rows.append((claim["product_id"], _WRITEBACK[claim["relation"]], claim["value"], claim["verdict"],
                     _EVIDENCE_FIELDS[claim["source"]], start, end, LLM_PROVENANCE))
    return rows


def write_attributes(db_path: Path, rows: Sequence[tuple[Any, ...]], rejected: Sequence[tuple[Any, ...]] = ()) -> None:
    """Replace earlier model rows; a rule-extracted value keeps its own evidence."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM product_attributes WHERE provenance = ?", (LLM_PROVENANCE,))
        conn.execute("DELETE FROM attribute_rejections WHERE provenance = ?", (LLM_PROVENANCE,))
        conn.executemany("INSERT OR IGNORE INTO product_attributes VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.executemany("INSERT OR IGNORE INTO attribute_rejections VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rejected)


def product_vectors(index_dir: Path = AMAZON_INDEX_DIR) -> tuple[list[str], np.ndarray]:
    from rag.vector_index import ChromaVectorIndex

    ids, vectors = ChromaVectorIndex(index_dir, AMAZON_PRODUCT_COLLECTION).vectors()
    return [item.removesuffix(":product") for item in ids], vectors


def driver_from_env() -> Any:
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
        connection_timeout=2.0,
        connection_acquisition_timeout=2.0,
        max_transaction_retry_time=0.0,
    )


def _write(session: Any, query: str, rows: Sequence[dict[str, Any]]) -> None:
    for start in range(0, len(rows), _BATCH):
        session.run(query, rows=list(rows[start:start + _BATCH])).consume()


def _load_structure(session: Any, catalog: Sequence[CatalogRow], devices: dict[str, str | None]) -> None:
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    _write(session, "UNWIND $rows AS row MERGE (c:Category {name: row.name}) SET c.kind = row.kind", [
        *[{"name": name, "kind": "top"} for name in sorted({row.category for row in catalog})],
        *[{"name": name, "kind": "group"} for name in sorted({row.group for row in catalog if row.group})],
        *[{"name": node["label"], "kind": "leaf"} for node in taxonomy["nodes"]],
    ])
    links = {(row.label, row.category) for row in catalog} | {(row.label, row.group) for row in catalog if row.group}
    _write(session, "UNWIND $rows AS row MATCH (a:Category {name: row.a}), (b:Category {name: row.b}) MERGE (a)-[:CHILD_OF]->(b)",
           [{"a": a, "b": b} for a, b in sorted(links)])
    _write(session, "UNWIND $rows AS row MATCH (a:Category {name: row.a}), (b:Category {name: row.b}) MERGE (a)-[:ACCESSORY_FOR]->(b)",
           [{"a": node["label"], "b": target} for node in taxonomy["nodes"] for target in node["accessory_for"]])
    _write(session, """
        UNWIND $rows AS row
        MERGE (p:Product {id: row.id}) SET p.title = row.title, p.label = row.label, p.role = row.role, p.device = row.device
        WITH p, row MATCH (c:Category {name: row.label}) MERGE (p)-[:IN_CATEGORY]->(c)
    """, [{"id": r.product_id, "title": r.title, "label": r.label, "role": r.role, "device": devices.get(r.product_id)} for r in catalog])
    _write(session, """
        UNWIND $rows AS row
        MATCH (p:Product {id: row.id}) MERGE (b:Brand {name: row.brand}) MERGE (p)-[:MADE_BY]->(b)
    """, [{"id": r.product_id, "brand": r.brand} for r in catalog if not r.generic_brand])


def _load_claims(session: Any, claims: Sequence[dict[str, Any]]) -> None:
    by_relation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        by_relation[claim["relation"]].append(claim)
    for relation, rows in by_relation.items():
        kind = RELATIONS[relation][0]
        keyed = [{**row, "key": entity_key(kind, row["value"])} for row in rows]
        # Relation and label names come from the RELATIONS whitelist, never from model output.
        _write(session, f"""
            UNWIND $rows AS row
            MATCH (p:Product {{id: row.product_id}})
            MERGE (e:Entity {{key: row.key}}) SET e:{_LABELS[kind]}, e.name = row.value
            MERGE (p)-[r:{relation}]->(e)
            SET r.source = row.source, r.quote = row.quote, r.soft = row.soft, r.rating = row.rating
        """, keyed)


def load_graph(
    driver: Any, catalog: Sequence[CatalogRow], claims: Sequence[dict[str, Any]], similar: Sequence[dict[str, Any]],
    devices: dict[str, str | None], digest: str, ontology_version: str,
) -> None:
    """Rebuild from empty. The version node goes last, so a partial load never looks complete."""
    with driver.session() as session:
        session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS").consume()
        for label, field in (("Product", "id"), ("Category", "name"), ("Brand", "name"), ("Entity", "key"), ("Community", "id")):
            session.run(f"CREATE CONSTRAINT {label.lower()}_{field} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{field} IS UNIQUE").consume()
        session.run("CREATE INDEX product_label IF NOT EXISTS FOR (p:Product) ON (p.label)").consume()
        session.run("CALL db.awaitIndexes(60)").consume()
        _load_structure(session, catalog, devices)
        _load_claims(session, claims)
        _write(session, """
            UNWIND $rows AS row
            MATCH (a:Product {id: row.a}), (b:Product {id: row.b})
            MERGE (a)-[r:SIMILAR_TO]->(b) SET r.score = row.score
        """, similar)
        session.run(
            "CREATE (:CatalogVersion {digest: $digest, ontology: $ontology, claims: $claims, built_at: $built_at})",
            digest=digest, ontology=ontology_version, claims=len(claims), built_at=datetime.now(timezone.utc).isoformat(),
        ).consume()


def catalog_digest() -> str:
    return json.loads(BUILD_REPORT_PATH.read_text(encoding="utf-8"))["content_digest"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=AMAZON_DB_PATH)
    parser.add_argument("--no-attributes", action="store_true", help="Leave product_attributes untouched.")
    parser.add_argument("--off-peak", action="store_true", help="Pause verification during double-priced hours.")
    parser.add_argument("--min-balance", type=float, default=2.0, help="Stop verification when the CNY balance reaches this.")
    args = parser.parse_args()

    ontology, products = load_ontology(), load_products(args.db)
    by_id = {product.product_id: product for product in products}
    catalog = load_catalog_rows(args.db)
    verifier = AttributeVerifier(before_chunk=spending_guard(args.off_peak, args.min_balance))
    claims, refused = verified_claims(valid_claims(load_cache(), products, ontology), by_id, verifier)
    print(f"verified claims: kept={len(claims)} refused={len(refused)}", flush=True)
    taken = {(c["product_id"], c["value"]) for c in claims if c["relation"] == "COMPATIBLE_WITH"}
    claims += rule_device_claims(args.db, by_id, taken)
    ids, vectors = product_vectors()
    similar = similar_pairs(
        ids, vectors, {row.product_id: row.group or row.label for row in catalog},
        {row.product_id: row.dup_key for row in catalog},
    )
    devices = {row.product_id: own_device(row.title) for row in catalog}
    print(f"products={len(catalog)} claims={len(claims)} similar={len(similar)}", flush=True)

    with driver_from_env() as driver:
        load_graph(driver, catalog, claims, similar, devices, catalog_digest(), ontology["version"])
    if not args.no_attributes:
        rows = attribute_rows(claims, by_id)
        write_attributes(args.db, rows, rejection_rows(refused, by_id))
        print(f"attribute rows written: {len(rows)}", flush=True)


if __name__ == "__main__":
    main()
