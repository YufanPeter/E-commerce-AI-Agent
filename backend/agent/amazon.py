"""English routing support and factual copy for the Amazon experience."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent.intent_router import IntentDecision
from agent.session import AgentSession
from agent.tools.base import ToolResult


def unrouted(query: str, session: AgentSession) -> IntentDecision:
    """Without the router a message is treated as a search; a follow-up gets a retry prompt instead."""
    follow_up = bool(session.recall_hits() or session.task.pending)
    return IntentDecision(
        tool="fallback" if follow_up else "recommend", rewritten_query=query, confidence="low",
        reasoning="router unavailable", args={"unavailable": True} if follow_up else {},
    )


def with_cheaper_bound(decision: IntentDecision, session: AgentSession) -> IntentDecision:
    """Turn "cheaper" into a price limit below everything just shown."""
    from store.product_store import ProductStore

    hits = ProductStore().get_products_by_ids([hit["product_id"] for hit in session.recall_hits()])
    if not hits:
        return decision
    bound = max(0, min(hit.base_price for hit in hits) - 0.01)
    return replace(decision, rewritten_query=f"Under ${bound:.2f}")


class CatalogMessageTool:
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        return ToolResult(tool_name=self.name, payload={}, narrative_override=self.message, needs_composer=False)


def reply(tool_result: ToolResult) -> str | dict[str, Any]:
    data = tool_result.payload
    if tool_result.tool_name == "product_detail":
        product = data.get("product") or {}
        if not product:
            return "I couldn't identify a product. Choose one from the latest results."
        evidence = data.get("evidence") or []
        focus = str(data.get("focus_aspect") or "")
        if focus in {"reviews", "negative_reviews"}:
            reviews = [ev for ev in evidence if ev.get("source_type") == "user_review"]
            if focus == "negative_reviews":
                reviews = [ev for ev in reviews if ev.get("metadata", {}).get("polarity") == "negative"]
            if reviews:
                quote = str(reviews[0].get("text") or "").strip()[:360]
                return f'One Amazon customer wrote: “{quote}” This is a sampled review, not a guarantee.'
            return "The catalog does not have a matching customer review for this product."
        description = next(
            (str(ev.get("text") or "").strip()[:420] for ev in evidence
             if ev.get("source_type") == "marketing_description"), "",
        )
        price = product.get("price_display") or ""
        lead = str(product.get("title") or "This product")
        listing = f" Listing description: {description}" if description else ""
        return f"{lead}. {price}.{listing} Prices and stock are simulated for this demo."
    if tool_result.tool_name == "compare":
        comparison = data.get("comparison") or {}
        if not comparison:
            return "Choose two products from the latest results to compare."
        return {
            "opening": "Here are the catalog facts side by side.",
            "items": [],
            "summary": "Ratings are historical. Prices and stock are simulated for this demo.",
            "followup": ["Show details for the first one"],
        }

    products = data.get("products") or []
    summary = data.get("summary") or {}
    if not products:
        if summary.get("needs_clarification"):
            return "What would you like to shop for? You can include a brand and a budget in USD."
        if "This catalog only supports USD budgets" in summary.get("unavailable_terms", []):
            return "This catalog uses USD reference prices. What is your budget in USD?"
        return "I didn't find a matching product in this catalog. Try a broader search or adjust the budget."
    items = []
    for card in products:
        details = (data.get("item_notes") or {}).get(card["product_id"]) or next(
            (str(ev.get("text") or "").strip() for ev in (card.get("evidence") or {}).get("points", []) if ev.get("text")), "",
        )
        items.append({
            "productId": card["product_id"],
            "description": details[:240] or f"From {card.get('brand') or 'this brand'} in {card.get('sub_category') or 'this category'}.",
        })
    return {
        "opening": data.get("opening") or f"I found {len(items)} matching options in the Amazon catalog.",
        "items": items,
        "summary": "Prices and stock are simulated for this demo.",
        "followup": ["Compare the first and second", "Show details for the first one"] if len(items) > 1
                    else ["Show details for the first one"],
    }
