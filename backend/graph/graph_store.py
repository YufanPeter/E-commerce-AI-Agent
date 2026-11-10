"""Read side of the knowledge graph.

Every method fails open. After an error the store stays quiet for a while, so an outage
costs one short timeout instead of slowing every turn. Results only name products; prices,
SKUs and stock must still come from SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import time
from typing import Any, Protocol


logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 1.5
_COOLDOWN_SECONDS = 30.0
_MAX_EVIDENCE = 3


@dataclass(frozen=True)
class GraphQuery:
    aspects: tuple[str, ...] = ()
    use_cases: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.aspects or self.use_cases or self.devices)


@dataclass(frozen=True)
class GraphHit:
    product_id: str
    score: float
    evidence: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AccessoryMatches:
    product_ids: tuple[str, ...] = ()
    device: str | None = None
    confirmed: bool = False
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)


class GraphStore(Protocol):
    def recall(self, query: GraphQuery, limit: int) -> list[GraphHit]: ...

    def similar(self, product_id: str, limit: int) -> list[str]: ...

    def accessories(self, product_id: str, limit: int) -> AccessoryMatches: ...

    def guide(self, label: str) -> dict[str, Any] | None: ...


def wanted_terms(query: GraphQuery) -> list[dict[str, Any]]:
    """Graph lookups for one query. Reported criticism of a wanted aspect counts against a product."""
    wanted: list[dict[str, Any]] = []
    for aspect in query.aspects:
        wanted.append({"key": f"aspect:{aspect}", "relation": "PRAISED_FOR", "weight": 1.0})
        wanted.append({"key": f"aspect:{aspect}", "relation": "CRITICIZED_FOR", "weight": -1.0})
    for use_case in query.use_cases:
        wanted.append({"key": f"use_case:{use_case}", "relation": "SUITED_FOR_USE", "weight": 1.0})
    for device in query.devices:
        wanted.append({"key": f"device:{device}", "relation": "COMPATIBLE_WITH", "weight": 3.0})
        if device.startswith("iphone_"):
            wanted.append({"key": "device:iphone", "relation": "COMPATIBLE_WITH", "weight": 1.0})
    return wanted


_RECALL = """
UNWIND $wanted AS want
MATCH (e:Entity {key: want.key})<-[r]-(p:Product)
WHERE type(r) = want.relation AND ($labels = [] OR p.label IN $labels)
WITH p, want.weight AS weight,
     {relation: type(r), value: e.name, source: r.source, quote: r.quote, soft: r.soft} AS claim
WITH p, sum(weight) AS score, collect(CASE WHEN weight > 0 THEN claim END) AS claims
WHERE score > 0
RETURN p.id AS id, score, claims[..$evidence] AS evidence
ORDER BY score DESC, id
LIMIT $limit
"""

_SIMILAR = """
MATCH (:Product {id: $id})-[r:SIMILAR_TO]->(other:Product)
RETURN other.id AS id ORDER BY r.score DESC, id LIMIT $limit
"""

_OWN_DEVICE = "MATCH (p:Product {id: $id}) RETURN p.device AS device"

_ACCESSORIES = """
MATCH (:Product {id: $id})-[:IN_CATEGORY]->(:Category)<-[:ACCESSORY_FOR]-(:Category)<-[:IN_CATEGORY]-(item:Product)
OPTIONAL MATCH (item)-[r:COMPATIBLE_WITH]->(d:Device) WHERE d.name IN $devices
WITH item, head(collect({device: d.name, source: r.source, quote: r.quote})) AS match
WHERE $devices = [] OR match.device IS NOT NULL
RETURN item.id AS id, match
ORDER BY id LIMIT $limit
"""

_GUIDE = "MATCH (c:Community {id: $id}) RETURN c.guide AS guide"
_VERSION = "MATCH (v:CatalogVersion) RETURN v.digest AS digest"


class Neo4jGraphStore:
    def __init__(self, driver: Any, expected_digest: str | None = None) -> None:
        self._driver = driver
        self._expected_digest = expected_digest
        self._version_checked = expected_digest is None
        self._quiet_until = 0.0

    def _read(self, text: str, **parameters: Any) -> list[dict[str, Any]]:
        from neo4j import Query

        records, _, _ = self._driver.execute_query(Query(text, timeout=_TIMEOUT_SECONDS), parameters_=parameters)
        return [record.data() for record in records]

    def _ready(self) -> bool:
        """A graph built from another catalog version would name the wrong products."""
        if self._version_checked:
            return True
        rows = self._read(_VERSION)
        if not rows or rows[0]["digest"] != self._expected_digest:
            logger.warning("Knowledge graph does not match this catalog; the graph channel stays off.")
            self._quiet_until = float("inf")
            return False
        self._version_checked = True
        return True

    def _safely(self, empty: Any, reader: Any) -> Any:
        if time.monotonic() < self._quiet_until:
            return empty
        try:
            return reader() if self._ready() else empty
        except Exception as exc:  # noqa: BLE001 - the graph only adds to a working search
            logger.warning("Knowledge graph unavailable, continuing without it: %r", exc)
            self._quiet_until = time.monotonic() + _COOLDOWN_SECONDS
            return empty

    def recall(self, query: GraphQuery, limit: int) -> list[GraphHit]:
        if not query:
            return []
        return self._safely([], lambda: [
            GraphHit(row["id"], float(row["score"]), tuple(row["evidence"]))
            for row in self._read(_RECALL, wanted=wanted_terms(query), labels=list(query.labels), evidence=_MAX_EVIDENCE, limit=limit)
        ])

    def similar(self, product_id: str, limit: int) -> list[str]:
        return self._safely([], lambda: [row["id"] for row in self._read(_SIMILAR, id=product_id, limit=limit)])

    def accessories(self, product_id: str, limit: int) -> AccessoryMatches:
        def read() -> AccessoryMatches:
            focus = self._read(_OWN_DEVICE, id=product_id)
            device = focus[0]["device"] if focus else None
            rows = self._read(_ACCESSORIES, id=product_id, devices=[device] if device else [], limit=limit) if device else []
            if not rows:  # Nothing states this device, so show the category unconfirmed.
                rows = self._read(_ACCESSORIES, id=product_id, devices=[], limit=limit)
                return AccessoryMatches(tuple(row["id"] for row in rows), device, False)
            evidence = {row["id"]: row["match"] for row in rows if row["match"].get("device")}
            return AccessoryMatches(tuple(row["id"] for row in rows), device, True, evidence)

        return self._safely(AccessoryMatches(), read)

    def guide(self, label: str) -> dict[str, Any] | None:
        def read() -> dict[str, Any] | None:
            rows = self._read(_GUIDE, id=f"leaf:{label}")
            return json.loads(rows[0]["guide"]) if rows and rows[0]["guide"] else None

        return self._safely(None, read)


def _digest_from_report() -> str | None:
    from graph.build_graph import catalog_digest

    try:
        return catalog_digest()
    except (OSError, KeyError, ValueError):
        return None


def open_graph_store() -> GraphStore | None:
    """``None`` when the graph is switched off. Connections are made on first use."""
    if os.getenv("CARTPILOT_GRAPH", "1").strip() != "1" or not os.getenv("NEO4J_PASSWORD"):
        return None
    try:
        from graph.build_graph import driver_from_env

        return Neo4jGraphStore(driver_from_env(), _digest_from_report())
    except Exception as exc:  # noqa: BLE001 - a missing driver must not stop the catalog
        logger.warning("Knowledge graph disabled: %r", exc)
        return None


