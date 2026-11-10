"""English routing and factual copy for the offline Amazon experience."""

from __future__ import annotations

import re
from typing import Any

from agent.intent_router import IntentDecision
from agent.session import AgentSession
from agent.tools.base import ToolResult


def route_catalog(query: str, session: AgentSession) -> IntentDecision:
    text = query.strip().lower()
    if re.search(r"\b(cart|checkout|check out|place (?:an |the )?order|buy (?:it|this|that) now)\b", text):
        tool = "cart"
    elif re.search(r"\b(compare|versus|vs\.?|differences?|which is better)\b", text):
        tool = "compare"
    elif re.search(r"\b(details?|reviews?|tell me about|previous one|next one|the last one)\b", text):
        tool = "product_detail"
    elif session.recall_parsed() and re.search(
        r"^(?:under|below|over|between|up to|within|only|by|no|not|without|exclude|avoid|any brand|clear|budget|"
        r"same brand|make it|keep|just|instead)\b|\b(?:cheaper|more expensive|any price)\b", text,
    ):
        tool = "refine"
    elif text.strip(" !?.") in {"", "hi", "hey", "hello", "help", "recommend", "recommend something", "just browsing"}:
        tool = "clarify"
    elif re.search(r"\b(weather|stock market|write (?:me )?(?:a poem|code))\b", text):
        tool = "fallback"
    else:
        tool = "recommend"
    rewritten = query
    if tool == "refine" and "cheaper" in text and "$" not in text:
        from store.product_store import ProductStore

        hits = ProductStore().get_products_by_ids([h["product_id"] for h in session.recall_hits()])
        if hits:
            bound = max(0, min(h.base_price for h in hits) - 0.01)
            rewritten = f"Under ${bound:.2f}"
    return IntentDecision(tool=tool, rewritten_query=rewritten, confidence="high", reasoning="English catalog routing")


class CatalogMessageTool:
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message

    def run(self, query: str, session: AgentSession, slots: dict[str, Any]) -> ToolResult:
        return ToolResult(tool_name=self.name, payload={}, narrative_override=self.message, needs_composer=False)


def reply(tool_result: ToolResult) -> str | dict[str, Any]:
    data = tool_result.payload
    if data.get("error"):
        return "I couldn't complete that request. Please try again."
    if tool_result.narrative_override is not None:
        # Keep the English copy of supported outcomes. Older tools contain demo-only Chinese copy.
        text = tool_result.narrative_override
        if not re.search(r"[\u4e00-\u9fff]", text):
            return text
        if tool_result.tool_name == "compare":
            return "Which two products should I compare? Try “Compare the first and second.”"
        if tool_result.tool_name == "product_detail":
            return "Which product do you mean? Try “Show details for the second one.”"
        return "Tell me what product you want to find and, if you have one, your budget in USD."
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
        return f"{lead}. {price}.{listing} Current inventory and purchase options are unknown."
    if tool_result.tool_name == "compare":
        comparison = data.get("comparison") or {}
        if not comparison:
            return "Choose two products from the latest results to compare."
        return {
            "opening": "Here are the catalog facts side by side.",
            "items": [],
            "summary": "Prices and ratings are historical. Current availability is unknown.",
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
        details = next((str(ev.get("text") or "").strip() for ev in (card.get("evidence") or {}).get("points", [])
                        if ev.get("text")), "")
        items.append({
            "productId": card["product_id"],
            "description": details[:240] or f"From {card['brand']} in {card['sub_category']}.",
        })
    return {
        "opening": f"I found {len(items)} matching options in the Amazon catalog.",
        "items": items,
        "summary": "Prices are historical reference prices in USD.",
        "followup": ["Compare the first and second", "Show details for the first one"] if len(items) > 1
                    else ["Show details for the first one"],
    }
