"""Clean review text scraped from Amazon before anyone reads it.

The source keeps Amazon's page markup in review bodies: an embedded video is a
``[[VIDEOID:<hex>]]`` placeholder, a link to another listing is
``[[ASIN:<id> <product name>]]``, and quotes arrive as HTML escapes such as ``&#34;``.
"""

from __future__ import annotations

import html
import re

_VIDEO = re.compile(r"\[\[VIDEOID:[^\]]*\]\]")
# A linked listing keeps its product name, which is what the reviewer wrote.
_LINKED_LISTING = re.compile(r"\[\[ASIN:\s*\S+\s*([^\]]*)\]\]")
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SPACES = re.compile(r"[ \t]{2,}")


def clean_review_text(text: str | None) -> str:
    """Return the review as a reader would see it on the product page."""
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = _VIDEO.sub(" ", cleaned)
    cleaned = _LINKED_LISTING.sub(lambda match: match.group(1).strip(), cleaned)
    cleaned = _LINE_BREAK.sub("\n", cleaned)
    cleaned = _SPACES.sub(" ", cleaned)
    return re.sub(r" +([.,!?])", r"\1", cleaned).strip()
