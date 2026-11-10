"""Locale-specific copy, selected once per process from the active catalog profile."""

from __future__ import annotations

from typing import Any

from catalog_config import PROFILE
from i18n import en, zh


_MESSAGES: dict[str, dict[str, str]] = {"en": en.MESSAGES, "zh": zh.MESSAGES}
_ACTIVE = _MESSAGES[PROFILE.locale]


def t(key: str, /, **values: Any) -> str:
    """Return the active locale's message; a missing key fails loudly."""
    template = _ACTIVE[key]
    return template.format(**values) if values else template


def is_english() -> bool:
    return PROFILE.locale == "en"


def format_money(amount: float) -> str:
    if PROFILE.currency == "USD":
        return f"${amount:,.2f}"
    return f"¥{amount:g}"
