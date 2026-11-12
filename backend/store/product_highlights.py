"""Short "About this item" points written from the seller's listing.

Seller text runs to hundreds of words in whatever style the seller chose (checkmarks, music
notes, 【headers】, run-on paragraphs). The detail page shows the same shape for every product
instead: three to five points, each an emoji, a few-word title and one short line.

Points come from the model, are checked for shape, and are cached per product together with a
hash of the listing they came from, so an edited listing is rewritten and nothing else is.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

MAX_POINTS = 5
MIN_POINTS = 2
_TITLE_WORDS = 6
_DETAIL_WORDS = 20

SYSTEM = (
    "You turn a product listing into short points for a shopping app's \"About this item\" section.\n"
    "Write 3 to 5 points, the ones a shopper decides on first (what it does, key specs, fit or size, "
    "what is in the box, limits the listing admits).\n"
    "Each point has:\n"
    "- emoji: one emoji that pictures the point (🔊 sound, 💧 water resistance, 🔋 battery, 📏 size). "
    "Never a checkmark, star or arrow, and never the same emoji twice.\n"
    "- title: 2 to 5 words, sentence case, no ending period.\n"
    "- detail: one plain sentence of at most 16 words with the concrete fact (numbers, materials, ratings).\n"
    "Use only facts in the listing. No prices, no hype words (amazing, perfect, ultimate), no repeated points. "
    "Keep a limitation if the listing states one (\"not fully submersible\")."
)

_TOOL = {
    "type": "function",
    "function": {
        "name": "write_points",
        "description": "Short points for the About this item section.",
        "parameters": {
            "type": "object",
            "properties": {
                "points": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": MAX_POINTS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "emoji": {"type": "string"},
                            "title": {"type": "string"},
                            "detail": {"type": "string"},
                        },
                        "required": ["emoji", "title", "detail"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["points"],
            "additionalProperties": False,
        },
    },
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS product_highlights (
    product_id  TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    points_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def source_hash(title: str, listing: str) -> str:
    return hashlib.sha256(f"{title}\n{listing}".encode("utf-8")).hexdigest()[:16]


def clean_points(raw: Any) -> list[dict[str, str]]:
    """Keep well-formed points only; a malformed answer yields nothing rather than odd text."""
    points: list[dict[str, str]] = []
    seen_emoji: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        emoji = str(item.get("emoji") or "").strip()
        title = " ".join(str(item.get("title") or "").split()).rstrip(".")
        detail = " ".join(str(item.get("detail") or "").split())
        if not emoji or not title or not detail or len(emoji) > 4 or emoji.isascii():
            continue
        if emoji in {"✅", "✔️", "✔", "⭐", "➡️"} or emoji in seen_emoji:
            continue
        if len(title.split()) > _TITLE_WORDS or len(detail.split()) > _DETAIL_WORDS:
            continue
        seen_emoji.add(emoji)
        points.append({"emoji": emoji, "title": title, "detail": detail})
    points = points[:MAX_POINTS]
    return points if len(points) >= MIN_POINTS else []


def write_points(title: str, listing: str, timeout: float = 20.0) -> list[dict[str, str]]:
    """Ask the model for points; any failure returns an empty list."""
    from llm.client import get_client, get_model_id
    from search.llm_parser import _extract_tool_arguments

    try:
        response = get_client().chat.completions.create(
            model=get_model_id(role="fast"),
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Product: {title}\n\nListing:\n{listing[:3000]}"},
            ],
            tools=[_TOOL],
            tool_choice={"type": "function", "function": {"name": "write_points"}},
            temperature=0.2,
            timeout=timeout,
        )
        return clean_points(_extract_tool_arguments(response).get("points"))
    except Exception as exc:  # noqa: BLE001 - the page falls back to the listing text
        logger.warning("Writing highlights failed: %r", exc)
        return []


class HighlightStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(_CREATE_SQL)
        return conn

    def cached(self, product_id: str, digest: str) -> list[dict[str, str]] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT points_json FROM product_highlights WHERE product_id = ? AND source_hash = ?",
                (product_id, digest),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, product_id: str, digest: str, points: list[dict[str, str]]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO product_highlights (product_id, source_hash, points_json) VALUES (?, ?, ?)",
                (product_id, digest, json.dumps(points, ensure_ascii=False)),
            )

    def points_for(self, product_id: str, title: str, listing: str) -> list[dict[str, str]]:
        """Cached points, or new ones written now. Empty points are not cached, so a
        model outage is retried on the next visit."""
        if not listing.strip():
            return []
        digest = source_hash(title, listing)
        cached = self.cached(product_id, digest)
        if cached is not None:
            return cached
        points = write_points(title, listing)
        if points:
            self.save(product_id, digest, points)
        return points
