from __future__ import annotations

"""Lightweight reference parsing for conversational tool targets."""

import re


_CN_NUM = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_ORDINAL_RE = re.compile(r"第\s*([0-9]+|[一二两三四五六七八九十])\s*(?:个|款|件|种)?")
_BARE_NUM_RE = re.compile(r"(?<!\d)([1-9])(?!\d)")


def _to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)


def resolve_indices(query: str, hit_count: int) -> list[int]:
    """Parse a user utterance into 0-based indices over recent hits."""
    if hit_count <= 0:
        return []

    text = query or ""
    if any(word in text for word in ("全部", "所有", "都对比", "都看看")):
        return list(range(hit_count))

    front = re.search(r"前\s*([0-9]+|[一二两三四五六七八九十])", text)
    if front:
        n = _to_int(front.group(1)) or 0
        return list(range(min(n, hit_count)))

    if any(word in text for word in ("这俩", "俩", "两个", "两款", "二者")):
        return list(range(min(2, hit_count)))

    indices: list[int] = []
    for match in _ORDINAL_RE.finditer(text):
        n = _to_int(match.group(1))
        if n and 1 <= n <= hit_count and (n - 1) not in indices:
            indices.append(n - 1)

    if not indices:
        for match in _BARE_NUM_RE.finditer(text):
            n = _to_int(match.group(1))
            if n and 1 <= n <= hit_count and (n - 1) not in indices:
                indices.append(n - 1)

    return indices
