"""Answers that walk the knowledge graph: similar items, accessories and buying guides.

The graph only says which products are related. Titles and prices on the cards are read
from SQLite, so a stale graph can never show a wrong price.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.recommend import _MAX_PER_BRAND, RecommendTool, _canonical_brand, _card_with_evidence, _diversify
from agent.tools.resolve import focus_index_in, positions_in, resolve_one
from search.catalog_understanding import understand
from search.query_understanding import ParsedQuery


_SHOWN = 5
_POOL = 60
_PRICE_NOTE = "Prices and stock are simulated for this demo."
_COMPARE_HINT = (
    "Say what a product shares with the original only from that product's own title and evidence, "
    "and never claim a feature is missing or unconfirmed across all of them."
)


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
        if self.name == "resume":
            return self._resume(query, session, service, slots)
        focus = self._focus(query, session, slots)
        if focus is None:
            if not getattr(session, "product_trail", None):
                return _text_result(self.name, "I don't have any products you bought or opened before yet. Tell me what you're looking for and I'll find options.")
            return _text_result(self.name, "Which product do you mean? Try “the first one”.")
        return self._accessories(focus, session, service) if self.name == "accessories" else self._similar(query, focus, session, service, slots)

    @staticmethod
    def _focus(query: str, session: AgentSession, slots: dict[str, Any]) -> dict[str, Any] | None:
        """A product from an earlier visit, the named product, the one under discussion, then the top result."""
        if slots.get("history_product"):
            return slots["history_product"]
        hits = session.recall_hits()
        if not hits:
            return None
        focus_index = focus_index_in(hits, session)
        named = positions_in(query, hits, session, slots)
        index = named[0] if named else resolve_one(query, hits, use_llm=False, focus_index=focus_index)
        return hits[next(i for i in (index, focus_index, 0) if i is not None)]

    def _cards(
        self, hits: list[Any], query: str, session: AgentSession, opening: str,
        notes: dict[str, str] | None = None, constraints: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Result in the search shape, so follow-ups such as "the second one" keep working.

        ``constraints`` describe what is now shown; follow-ups such as "cheaper ones" refine it.
        """
        shown = constraints if constraints is not None else (session.recall_parsed() or {})
        session.remember_search(shown, [{"product_id": h.product_id, "title": h.title} for h in hits])
        products = [_card_with_evidence(h, query) for h in hits]
        self._recommend._attach_price_displays(products, None)
        payload = {
            "query": query, "products": products, "opening": opening, "item_notes": notes or {},
            "summary": {"hit_count": len(hits), "needs_clarification": False, "unavailable_terms": []},
            # Not a search the shopper typed, so the search history leaves it out.
            "source": self.name,
        }
        return ToolResult(tool_name="recommend", payload=payload, composer_hint=opening)

    def _similar(self, query: str, focus: dict[str, Any], session: AgentSession, service: Any, slots: dict[str, Any]) -> ToolResult:
        title, wants_cheaper = focus["title"], bool(slots.get("cheaper"))
        price = service.min_price(focus["product_id"])
        limit = price - 0.01 if wants_cheaper and price is not None else None
        related = service.graph.similar(focus["product_id"], _POOL) if service.graph is not None else []
        if not related:  # Without the graph, a search near the product itself still answers.
            return self._like(focus, session, slots, limit)
        # Neighbours in the graph are often the same seller's other models; two per brand
        # leaves room for real alternatives.
        hits = _diversify(service.hits_for(related, max_price=limit), _SHOWN, [(lambda h: _canonical_brand(h.brand), _MAX_PER_BRAND)])
        if not hits:
            return _text_result(self.name, f"I couldn't find anything similar and cheaper than {title}.")
        lead = "cheaper options" if wants_cheaper else "similar options"
        result = self._cards(hits, query, session, f"Here are {lead} based on {title}.")
        # The original's title once led to "none is confirmed to be aluminum" over a list
        # whose first title said Aluminum.
        return replace(result, composer_hint=f"{result.composer_hint} {_COMPARE_HINT}")

    def _like(self, focus: dict[str, Any], session: AgentSession, slots: dict[str, Any], limit: float | None) -> ToolResult:
        """Products of the focus product's own type whose listings read like it.

        The type comes from the product, not from the last search: after "coffee mugs",
        "something like the headphones I added" still means headphones.
        """
        title = str(focus["title"])
        detail = self._recommend._get_products().get_product_detail(focus["product_id"])
        label = getattr(detail, "sub_category", None) or focus.get("sub_category")
        if not label:
            return self._recommend.run(title, session, slots)
        parsed = ParsedQuery(
            original_query=title, sub_category=label, retrieval_query=title, search_text=title, max_price=limit,
        )
        result = self._recommend._run_single(
            title, session, top_k=_SHOWN, base=None, parsed=parsed, exclude_ids={focus["product_id"]}, diversify=True,
        )
        remembered = slots.get("history_product")
        when = f", which the shopper {remembered['action']} on {remembered['at']}" if remembered else ""
        note = f"These are {label} similar to “{title}”{when}. Open by naming that product in a few words. {_COMPARE_HINT}"
        return replace(result, composer_hint=f"{note} {result.composer_hint or ''}".strip())

    def _accessories(self, focus: dict[str, Any], session: AgentSession, service: Any) -> ToolResult:
        title = focus["title"]
        matches = service.graph.accessories(focus["product_id"], _POOL) if service.graph is not None else None
        if matches is None or not matches.product_ids:
            return _text_result(self.name, f"I don't have accessory information for {title}.")
        hits = service.hits_for(list(matches.product_ids), popular_first=True)[:_SHOWN]
        # Accessories are now on screen, so a follow-up refines them, not the earlier search.
        types = {hit.sub_category for hit in hits}
        label = types.pop() if len(types) == 1 else None
        shown = {"original_query": f"{label or 'accessories'} for {title}", "retrieval_query": label or "accessories", "sub_category": label}
        if not matches.confirmed:
            opening = f"These accessories are in the matching category, but their listings don't confirm they fit {title}. Check the listing before buying."
            return self._cards(hits, title, session, opening, constraints=shown)
        notes = {
            product_id: f"Listing mentions “{claim['quote']}”."
            for product_id, claim in matches.evidence.items() if claim.get("quote")
        }
        return self._cards(hits, title, session, f"These listings state compatibility with {title}.", notes, constraints=shown)

    def _resume(self, query: str, session: AgentSession, service: Any, slots: dict[str, Any]) -> ToolResult:
        """An earlier conversation, recapped, with what was on screen then back on screen.

        Its search conditions come back too, so "cheaper ones" or "the second one" carry on
        from where that conversation stopped.
        """
        convo = slots.get("history_conversation")
        if not convo:
            if not getattr(session, "conversation_trail", None):
                return _text_result(self.name, "I don't have an earlier conversation with you yet. What are you shopping for?")
            return _text_result(self.name, "Which conversation do you mean? Tell me what we were looking for or roughly when.")
        facts = convo.get("facts") or {}
        recap = f"On {convo['at']} we talked about {convo['title']}: {convo['summary']}"
        ids = [p["product_id"] for p in facts.get("products") or [] if p.get("product_id")]
        hits = service.hits_for(ids)[:_SHOWN] if ids else []
        if not hits:
            return _text_result(self.name, f"{recap} Want to pick it up again?")
        note = (
            f"The shopper returned to an earlier conversation from {convo['at']}: {convo['title']}. {convo['summary']} "
            "Open by recapping it in one or two sentences in the second person (what they looked for and what they "
            "decided), with the date as a month and day, then say these are the products from then."
        )
        result = self._cards(hits, convo["title"], session, note, constraints=facts.get("constraints") or {})
        return replace(result, composer_hint=note)

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
    return {name: KnowledgeTool(name, recommend) for name in ("similar", "accessories", "buying_guide", "resume")}
