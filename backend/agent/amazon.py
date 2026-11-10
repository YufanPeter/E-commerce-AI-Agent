"""English routing and factual copy for the offline Amazon experience."""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Any

from agent.intent_router import IntentDecision
from agent.session import AgentSession
from i18n import en as english
from agent.tools.base import ToolResult


_CART_WORDS = re.compile(r"\b(cart|checkout|check out|place (?:an |the |my )?order|buy (?:it|this|that) now)\b")
_CART_ADD = re.compile(
    r"^(?:please\s+)?(?:add|put)\b"
    r"|^(?:please\s+)?(?:buy|get|take|order|i'?ll take)\s+(?:\d+\s+|two\s+|three\s+)?(?:of\s+)?(?:the\s+)?"
    r"(?:first|second|third|fourth|fifth|last|this|that|it|these|those)\b"
)


def _is_cart_request(text: str) -> bool:
    return bool(
        _CART_WORDS.search(text) or _CART_ADD.search(text) or english.ORDER_HISTORY_RE.search(text)
        or (english.REORDER_RE.search(text) and not english.NOT_REORDER_RE.search(text))
    )


def knowledge_route(query: str, session: AgentSession) -> str | None:
    """The tool for a graph-shaped question; the first two need a product on screen."""
    if english.GUIDE_RE.search(query):
        return "buying_guide"
    if not session.recall_hits():
        return None
    if english.ACCESSORY_RE.search(query):
        return "accessories"
    # The graph tool cannot apply a stated budget, so those requests go to normal routing.
    return "similar" if english.SIMILAR_RE.search(query) and not english.EXPLICIT_PRICE_RE.search(query) else None


def route_catalog(query: str, session: AgentSession) -> IntentDecision:
    text = query.strip().lower()
    if _is_cart_request(text):
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
    decision = IntentDecision(tool=tool, rewritten_query=query, confidence="high", reasoning="English catalog routing")
    return with_cheaper_bound(decision, query, session) if tool == "refine" else decision


def with_cheaper_bound(decision: IntentDecision, query: str, session: AgentSession) -> IntentDecision:
    """Turn "cheaper" into a price limit below everything just shown."""
    if "cheaper" not in query.lower() or "$" in query:
        return decision
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
            "description": details[:240] or f"From {card['brand']} in {card['sub_category']}.",
        })
    return {
        "opening": data.get("opening") or f"I found {len(items)} matching options in the Amazon catalog.",
        "items": items,
        "summary": "Prices and stock are simulated for this demo.",
        "followup": ["Compare the first and second", "Show details for the first one"] if len(items) > 1
                    else ["Show details for the first one"],
    }
