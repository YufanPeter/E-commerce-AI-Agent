"""English user-facing copy and prompts for the Amazon catalog."""

import re

MESSAGES: dict[str, str] = {
    "status.thinking": "Thinking…",
    "status.vision": "Reading the photo…",
    "status.visual_search": "Searching for products matching the photo description…",
    "image.display": "[Photo search]",
    "image.display_hint": "[Photo] {hint}",
    "image.unrecognized": "I couldn't identify the product. Try a clearer photo or describe what you want.",
    "review.title": "{nickname} · {rating}/5",
    "memory.removed": "Memory removed",
    "memory.restored": "Memory restored",
    "home.keep_browsing": "Keep browsing {label}",
    "home.browse_query": "Browse {label}",
    "vision.user": "What product is shown? Return an English search phrase.",
    "clarify.prompt": "What are you shopping for? You can include a brand and a budget in USD.",
    "fallback.prompt": "I can help you search and compare products in the Amazon catalog.",
    'cart.error': '{detail}. Check your cart or choose another item.',
    'cart.no_match': "I couldn't find a product matching “{name}”. Try different words, or I can recommend a few.",
    'cart.ask_add': 'Which product should I add? Try “Add the first one to my cart”, or ask me for recommendations.',
    'cart.which_product': 'Which one would you like to add?\n{lines}\nReply with its number or brand, like “the first one”.',
    'cart.cancelled': "Okay, I won't add it. What else can I help with?",
    'cart.no_candidates': 'Which product would you like? I can recommend a few first.',
    'cart.no_sku': 'This product has no options available right now. Want to look at another one?',
    'cart.resolved_query': 'Add “{title}” to the cart',
    'cart.sku_lost': "I lost track of that product's options. Please tell me again which one to add.",
    'cart.added': 'Added “{title}”{spec} to your cart, quantity {quantity}. Your cart now has {count} item(s), total {money}.',
    'cart.ask_spec': 'Adding “{title}” to your cart. It comes in several options, so please choose the one you want.',
    'cart.ask_quantity': 'What quantity would you like? For example, “Change the first item to 2”.',
    'cart.quantity_removed': 'Removed “{title}” from your cart.',
    'cart.quantity_set': 'Updated “{title}” to quantity {quantity}.',
    'cart.confirm_total': ' Your cart total is {money}.',
    'cart.removed': 'Removed “{title}” from your cart. You have {count} item(s) left, total {money}.',
    'cart.empty': 'Your cart is empty. Want me to recommend something?',
    'cart.view': "Here's your cart:\n{rows}\nTotal: {money}. Would you like to check out?",
    'cart.ordered': 'Order placed. Order number {order_id}, {count} item(s), total {money}, shipping to: {address}. Prices and stock are simulated for this demo.',
    'cart.no_orders': "I can't find any past orders. Tell me what you'd like to buy and I'll find it.",
    'cart.reorder_not_found': "I couldn't find “{name}” in your orders. You recently bought: {titles}. Which would you like again?",
    'cart.reorder_row': '{index}. {title}{spec} · last price {price}',
    'cart.which_reorder': 'Which one would you like to buy again?\n{rows}\nReply with its number or name.',
    'cart.price_same': 'the current price is {price}, the same as last time',
    'cart.price_lower': 'the current price is {price}, {diff} less than last time',
    'cart.price_higher': 'the current price is {price}, {diff} more than last time',
    'cart.reorder_spec_gone': 'The option you bought last time{spec} is no longer available. ',
    'cart.spec_word': '',
    'cart.reorder_delisted': '“{title}” is no longer available. Want me to find something similar?',
    'cart.reordered': 'Added your previous “{title}”{spec} × {quantity} to the cart; {note}. Cart total is {money}. Ready to check out?',
    'cart.whole_order_gone': 'Everything from your last order is no longer available. Want me to find something similar?',
    'cart.whole_order_added': 'Added {count} item(s) from your last order. Cart total is {money} (last order was {previous_money}). ',
    'cart.whole_order_skipped': "“{titles}” is no longer available and wasn't added. ",
    'cart.ask_checkout': 'Ready to check out?',
    'cart.no_history': "You haven't placed any orders yet. What would you like to shop for?",
    'cart.history_more': ' and {count} items in total',
    'cart.history_row': '{date}: {names}{more}, total {total}',
    'cart.history': "Your recent orders:\n{rows}\nTell me if you'd like to buy any of them again.",
    'cart.spec_suffix': ' ({spec})',
    'cart.default_address': 'Default address (1 Example Street, Seattle, WA 98101 · Demo User)',
    'cart.error.no_sku': 'Product {product_id} has no purchasable option',
    'cart.error.no_line': 'Cart line {cart_item_id} was not found',
    'cart.error.nothing_to_order': 'There is nothing in your cart to order',
    'cart.error.delisted': 'That option of “{title}” is no longer available; please remove it from your cart',
    'cart.error.short_stock': 'Not enough stock for “{title}”: only {stock} left',
    'cart.error.sold_out': 'Sorry, that option is out of stock or short: only {stock} left. Please choose another option.',
    'cart.error.empty': 'Your cart is empty',
    'cart.error.no_index': 'There is no item {index} in your cart',
    "cart.unavailable": "This catalog does not support adding items to the cart.",
}

TOOL_STEPS: dict[str, str] = {
    "recommend": "Searching the catalog…",
    "refine": "Updating your search…",
    "compare": "Comparing products…",
    "product_detail": "Checking the product details…",
    "cart": "Checking purchase options…",
}
DEFAULT_TOOL_STEP = "Checking your request…"
COMPOSE_STEP = "Preparing your results…"

VISION_SYSTEM = (
    "Return one short English shopping search phrase for the main product in the photo. "
    "Include the product type and only visible attributes. Ignore the background. "
    "If no product is recognizable, return UNRECOGNIZED. Output only the phrase."
)

# --- Cart intents -----------------------------------------------------------------------

CANCEL_RE = re.compile(r"\b(?:cancel|never ?mind|forget it|don'?t add|skip it|no thanks)\b", re.I)
ORDER_HISTORY_RE = re.compile(
    r"\b(?:order history|my (?:past |previous |recent )?orders?|past orders?|previous orders?|what did i (?:buy|order|purchase))\b", re.I
)
REORDER_RE = re.compile(r"\b(?:buy|order|get|purchase) .{0,30}\bagain\b|\breorder\b|\brepurchase\b", re.I)
NOT_REORDER_RE = re.compile(r"\b(?:review|compare|return|refund|similar|instead of)\b", re.I)
WHOLE_ORDER_RE = re.compile(r"\b(?:whole|entire|last|previous) order\b|\beverything\b|\ball of (?:it|them)\b", re.I)
REORDER_FILLER_RE = re.compile(
    r"\b(?:buy|order|get|purchase|reorder|repurchase|again|last|time|previous|that|those|the|one|ones|same|my|i|what|bought|please|whole|entire|everything|all|of|it|them)\b|\d+",
    re.I,
)
_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_QUANTITY = re.compile(
    r"\b(?:add|buy|get|order)\s+(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\b"
    r"|\b(\d{1,2})\s*(?:x\b|of (?:these|those|them|it)\b|pieces?\b|units?\b|items?\b)"
    r"|\b(?:qty|quantity)\s*(?:of|:|=)?\s*(\d{1,2})\b"
    r"|\bto\s+(\d{1,2})\s*$",
    re.I,
)
_CHECKOUT = re.compile(r"\b(?:check ?out|place (?:an |the |my )?order|buy (?:it|this|that|these) now|complete (?:my |the )?purchase|pay now)\b", re.I)
_REMOVE = re.compile(r"\b(?:remove|delete|take out|drop)\b", re.I)
_SET_QUANTITY = re.compile(r"\b(?:change|set|update|make)\b.*(?:\bquantity\b|\bqty\b|\bto\s+\d{1,2}\s*$)", re.I)
_VIEW = re.compile(r"\b(?:show|view|see|open|what'?s in|check)\b.*\bcart\b|^\s*(?:my )?cart\s*$|\bcart total\b", re.I)
_ADD = re.compile(r"\b(?:add|put|buy|get|grab|i'?ll take|take)\b", re.I)
_ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
_CART_ORDINAL = re.compile(r"\b(first|second|third|fourth|fifth)\b|\bitem\s+(\d{1,2})\b", re.I)


def quantity_in(text: str) -> int | None:
    match = _QUANTITY.search(text)
    if match is None:
        return None
    raw = next(group for group in match.groups() if group)
    return int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw.lower())


def _cart_index(text: str) -> int | None:
    match = _CART_ORDINAL.search(text)
    if match is None:
        return None
    return _ORDINAL[match[1].lower()] if match[1] else int(match[2])


def cart_action(text: str) -> tuple[str, dict[str, object]] | None:
    """Deterministic intent for clear English cart requests; None defers to the model."""
    if _CHECKOUT.search(text):
        return "checkout", {}
    if _REMOVE.search(text):
        return "remove", {"cart_index": _cart_index(text)}
    if _SET_QUANTITY.search(text):
        return "set_quantity", {"cart_index": _cart_index(text), "quantity": quantity_in(text)}
    if _VIEW.search(text):
        return "view", {}
    if _ADD.search(text):
        return "add", {"quantity": quantity_in(text) or 1}
    return None
