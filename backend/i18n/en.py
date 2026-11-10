"""English user-facing copy and prompts for the Amazon catalog."""

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
    "cart.unavailable": (
        "Amazon snapshot products have no verified purchase options or stock, "
        "so they cannot be added to the cart."
    ),
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
