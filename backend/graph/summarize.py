"""Detect graph communities and write buying guides for leaf categories.

Guides only rephrase counted claims from the graph, and every piece of advice must name
one of those claims. Results are cached by their input, so a rerun pays only for changes.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from typing import Any

from graph.build_graph import driver_from_env
from graph.common import GRAPH_DIR, account_balance_cny, call_tool


PROMPT_VERSION = "guide-v1"
SUMMARIES_PATH = GRAPH_DIR / "summaries.jsonl"
MIN_COMMUNITY_PRODUCTS = 20
MAX_COMMUNITIES = 30
_TOP = 10
_MIN_MENTIONS = 2
_KNOWLEDGE_RELATIONS = (
    "MADE_OF", "CONTAINS", "FREE_OF", "HAS_ATTRIBUTE", "COMPATIBLE_WITH",
    "SUITED_FOR_USE", "SUITED_FOR_AUDIENCE", "PRAISED_FOR", "CRITICIZED_FOR",
)
_FACET_NAMES = {
    "PRAISED_FOR": "praised", "CRITICIZED_FOR": "criticized", "HAS_ATTRIBUTE": "features", "MADE_OF": "materials",
    "CONTAINS": "ingredients", "FREE_OF": "free_of", "SUITED_FOR_USE": "use_cases", "SUITED_FOR_AUDIENCE": "audiences",
}

_SYSTEM = """You write short buying guides for an online store. Call record_guide and never answer directly.

You receive counted facts from product listings and customer reviews for one product group.
Rules:
1. Use only the supplied facts. Add no brands, numbers, standards or general knowledge.
2. look_for lists what to check before buying. watch_out lists complaints reported by customers.
3. Every item's value must be copied exactly from the supplied facts.
4. clarify holds up to three short questions that would narrow the choice, based on the facts.
5. Write plain English. Do not mention counts.
"""

_TOOL = {
    "type": "function",
    "function": {
        "name": "record_guide",
        "description": "Record the buying guide.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Two sentences at most."},
                "look_for": {"type": "array", "items": {"$ref": "#/$defs/item"}},
                "watch_out": {"type": "array", "items": {"$ref": "#/$defs/item"}},
                "clarify": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "look_for", "watch_out", "clarify"],
            "additionalProperties": False,
            "$defs": {"item": {
                "type": "object",
                "properties": {"value": {"type": "string"}, "advice": {"type": "string"}},
                "required": ["value", "advice"],
                "additionalProperties": False,
            }},
        },
    },
}

_FACTS = """
MATCH (:Category {name: $label})<-[:IN_CATEGORY]-(p:Product)-[r]->(e:Entity)
WHERE type(r) IN $relations
WITH type(r) AS relation, e.name AS value, count(*) AS mentions
WHERE mentions >= $minimum
RETURN relation, value, mentions ORDER BY mentions DESC, value
"""

_COMMUNITY_FACTS = """
MATCH (p:Product {community: $community})-[r]->(e:Entity)
WHERE type(r) IN $relations
WITH type(r) AS relation, e.name AS value, count(*) AS mentions
WHERE mentions >= $minimum
RETURN relation, value, mentions ORDER BY mentions DESC, value
"""


def facets(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = _FACET_NAMES.get(row["relation"])
        if name and len(grouped.setdefault(name, [])) < _TOP:
            grouped[name].append({"value": row["value"], "mentions": row["mentions"]})
    return grouped


def validated_guide(arguments: dict[str, Any], facts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Drop advice that does not rest on a supplied fact."""
    known = {item["value"] for items in facts.values() for item in items}
    def grounded(items: Any) -> list[dict[str, str]]:
        return [
            {"value": item["value"], "advice": str(item.get("advice") or "").strip()}
            for item in items or []
            if isinstance(item, dict) and item.get("value") in known and str(item.get("advice") or "").strip()
        ]

    return {
        "summary": str(arguments.get("summary") or "").strip(),
        "look_for": grounded(arguments.get("look_for")),
        "watch_out": grounded(arguments.get("watch_out")),
        "clarify": [str(question).strip() for question in (arguments.get("clarify") or [])[:3] if str(question).strip()],
    }


def _load_cache() -> dict[str, dict[str, Any]]:
    if not SUMMARIES_PATH.exists():
        return {}
    records = (json.loads(line) for line in SUMMARIES_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
    return {record["key"]: record["guide"] for record in records}


def _key(name: str, facts: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([PROMPT_VERSION, name, facts], sort_keys=True).encode()).hexdigest()


def _write_guide(session: Any, community_id: str, kind: str, name: str, guide: dict[str, Any], size: int) -> None:
    session.run(
        "MERGE (c:Community {id: $id}) SET c.kind = $kind, c.name = $name, c.size = $size, c.guide = $guide",
        id=community_id, kind=kind, name=name, size=size, guide=json.dumps(guide),
    ).consume()


def detect_communities(session: Any) -> list[dict[str, Any]]:
    """Run Leiden over products and entities; returns communities large enough to describe."""
    labels = ["Product", "Material", "Ingredient", "Attribute", "Device", "UseCase", "Audience", "Aspect"]
    relations = {relation: {"orientation": "UNDIRECTED"} for relation in _KNOWLEDGE_RELATIONS}
    session.run("CALL gds.graph.drop('knowledge', false) YIELD graphName RETURN graphName").consume()
    session.run("CALL gds.graph.project('knowledge', $labels, $relations)", labels=labels, relations=relations).consume()
    session.run("CALL gds.leiden.write('knowledge', {writeProperty: 'community', randomSeed: 42, concurrency: 1})").consume()
    session.run("CALL gds.graph.drop('knowledge') YIELD graphName RETURN graphName").consume()
    session.run("MATCH (c:Community {kind: 'leiden'}) DETACH DELETE c").consume()
    return session.run("""
        MATCH (p:Product) WHERE p.community IS NOT NULL
        WITH p.community AS community, count(*) AS size, collect(p.label) AS labels
        WHERE size >= $minimum
        RETURN community, size, labels ORDER BY size DESC LIMIT $limit
    """, minimum=MIN_COMMUNITY_PRODUCTS, limit=MAX_COMMUNITIES).data()


def _name_of(labels: list[str]) -> str:
    return max(set(labels), key=lambda label: (labels.count(label), label))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--min-balance", type=float, default=2.0)
    args = parser.parse_args()

    if account_balance_cny() <= args.min_balance:
        raise SystemExit("Balance is at the floor; no summaries were generated.")
    cache = _load_cache()
    jobs: list[tuple[str, str, str, int, dict[str, Any]]] = []
    relations = list(_KNOWLEDGE_RELATIONS)
    with driver_from_env() as driver, driver.session() as session:
        leaves = session.run(
            "MATCH (c:Category {kind: 'leaf'})<-[:IN_CATEGORY]-(p:Product) RETURN c.name AS name, count(p) AS size ORDER BY name"
        ).data()
        for leaf in leaves:
            rows = session.run(_FACTS, label=leaf["name"], relations=relations, minimum=_MIN_MENTIONS).data()
            jobs.append((f"leaf:{leaf['name']}", "leaf", leaf["name"], leaf["size"], facets(rows)))
        for community in detect_communities(session):
            rows = session.run(_COMMUNITY_FACTS, community=community["community"], relations=relations, minimum=_MIN_MENTIONS).data()
            jobs.append((f"leiden:{community['community']}", "leiden", _name_of(community["labels"]), community["size"], facets(rows)))

        def make(job: tuple[str, str, str, int, dict[str, Any]]) -> tuple[str, dict[str, Any], str]:
            _, _, name, _, facts = job
            key = _key(name, facts)
            if key in cache:
                return key, cache[key], "cached"
            call = call_tool(_SYSTEM, f"Product group: {name}\nFacts: {json.dumps(facts)}", _TOOL)
            return key, validated_guide(call.arguments, facts), "new"

        created = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool, SUMMARIES_PATH.open("a", encoding="utf-8") as output:
            for job, (key, guide, status) in zip(jobs, pool.map(make, [job for job in jobs if job[4]])):
                if status == "new":
                    output.write(json.dumps({"key": key, "guide": guide}) + "\n")
                    output.flush()
                    created += 1
                _write_guide(session, job[0], job[1], job[2], guide, job[3])
        session.run("""
            MATCH (c:Community {kind: 'leaf'}) MATCH (leaf:Category {name: c.name}) MERGE (c)-[:SUMMARIZES]->(leaf)
        """).consume()
        session.run("""
            MATCH (n) WHERE n.community IS NOT NULL AND (n:Product OR n:Entity)
            MATCH (c:Community {id: 'leiden:' + toString(n.community)}) MERGE (n)-[:IN_COMMUNITY]->(c)
        """).consume()
    print(f"jobs={len(jobs)} generated={created}", flush=True)


if __name__ == "__main__":
    main()
