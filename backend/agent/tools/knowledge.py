"""Answers that walk the knowledge graph: similar items, accessories and buying guides.

The graph only says which products are related. Titles and prices on the cards are read
from SQLite, so a stale graph can never show a wrong price.
"""

from __future__ import annotations

from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.recommend import RecommendTool, _card_with_evidence
from agent.tools.resolve import focus_index_in, resolve_one
from i18n import en as english
from search.catalog_understanding import understand


_SHOWN = 5
_POOL = 60
_PRICE_NOTE = "Prices and stock are simulated for this demo."


def _text_result(tool: str, text: str) -> ToolResult:
    return ToolResult(tool_name=tool, payload={}, narrative_override=text, needs_composer=False)


class KnowledgeTool:
    def __init__(self, name: str, recommend: RecommendTool) -> None:
        self.name = name
        self._recommend = recommend

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        service = self._recommend._get_service()
        if self.name == "buying_guide":
            return self._guide(query, session, service)
        focus = self._focus(query, session)
        if focus is None:
            return _text_result(self.name, "Which product do you mean? Try “the first one”.")
        return self._accessories(focus, session, service) if self.name == "accessories" else self._similar(query, focus, session, service, slots)

    @staticmethod
    def _focus(query: str, session: AgentSession) -> dict[str, Any] | None:
        """The named product, then the one under discussion, then the top result."""
        hits = session.recall_hits()
        if not hits:
            return None
        focus_index = focus_index_in(hits, session)
        index = resolve_one(query, hits, use_llm=False, focus_index=focus_index)
        return hits[next(i for i in (index, focus_index, 0) if i is not None)]

    def _cards(self, hits: list[Any], query: str, session: AgentSession, opening: str, notes: dict[str, str] | None = None) -> ToolResult:
        """Result in the search shape, so follow-ups such as "the second one" keep working."""
        session.remember_search(session.recall_parsed() or {}, [{"product_id": h.product_id, "title": h.title} for h in hits])
        products = [_card_with_evidence(h, query) for h in hits]
        self._recommend._attach_price_displays(products, None)
        payload = {
            "query": query, "products": products, "opening": opening, "item_notes": notes or {},
            "summary": {"hit_count": len(hits), "needs_clarification": False, "unavailable_terms": []},
        }
        return ToolResult(tool_name="recommend", payload=payload, composer_hint=opening)

    def _similar(self, query: str, focus: dict[str, Any], session: AgentSession, service: Any, slots: dict[str, Any]) -> ToolResult:
        title, wants_cheaper = focus["title"], bool(english.CHEAPER_RE.search(query))
        price = service.min_price(focus["product_id"])
        limit = price - 0.01 if wants_cheaper and price is not None else None
        related = service.graph.similar(focus["product_id"], _POOL) if service.graph is not None else []
        if not related:  # Without the graph, an ordinary search still gives a useful answer.
            label = (session.recall_parsed() or {}).get("sub_category") or title
            fallback = f"{label} under ${limit:.2f}" if limit is not None else str(label)
            return self._recommend.run(fallback, session, slots)
        hits = service.hits_for(related, max_price=limit)[:_SHOWN]
        if not hits:
            return _text_result(self.name, f"I couldn't find anything similar and cheaper than {title}.")
        lead = "cheaper options" if wants_cheaper else "similar options"
        return self._cards(hits, query, session, f"Here are {lead} based on {title}.")

    def _accessories(self, focus: dict[str, Any], session: AgentSession, service: Any) -> ToolResult:
        title = focus["title"]
        matches = service.graph.accessories(focus["product_id"], _POOL) if service.graph is not None else None
        if matches is None or not matches.product_ids:
            return _text_result(self.name, f"I don't have accessory information for {title}.")
        hits = service.hits_for(list(matches.product_ids), popular_first=True)[:_SHOWN]
        if not matches.confirmed:
            opening = f"These accessories are in the matching category, but their listings don't confirm they fit {title}. Check the listing before buying."
            return self._cards(hits, title, session, opening)
        notes = {
            product_id: f"Listing mentions “{claim['quote']}”."
            for product_id, claim in matches.evidence.items() if claim.get("quote")
        }
        return self._cards(hits, title, session, f"These listings state compatibility with {title}.", notes)

    def _guide(self, query: str, session: AgentSession, service: Any) -> ToolResult:
        parsed = understand(query, service.vocabulary)
        label = parsed.sub_category or (session.recall_parsed() or {}).get("sub_category")
        if not label:
            return _text_result(self.name, "Which kind of product are you shopping for?")
        leaves = service.vocabulary.expand([label])
        guide = service.graph.guide(leaves[0]) if service.graph is not None and leaves else None
        if not guide:
            return _text_result(self.name, f"I don't have a buying guide for {label} yet, but I can search that category for you.")
        return _text_result(self.name, _guide_text(guide))


def _guide_text(guide: dict[str, Any]) -> str:
    parts = [guide.get("summary") or ""]
    for heading, items in (("What to look for", guide.get("look_for")), ("Common complaints", guide.get("watch_out"))):
        if items:
            parts.append(f"{heading}:\n" + "\n".join(f"- {item['value'].replace('_', ' ').capitalize()}: {item['advice']}" for item in items))
    if guide.get("clarify"):
        parts.append(guide["clarify"][0])
    parts.append("This guide is based on listings and customer reviews in this catalog.")
    return "\n\n".join(part for part in parts if part)


def knowledge_tools(recommend: RecommendTool) -> dict[str, KnowledgeTool]:
    return {name: KnowledgeTool(name, recommend) for name in ("similar", "accessories", "buying_guide")}
