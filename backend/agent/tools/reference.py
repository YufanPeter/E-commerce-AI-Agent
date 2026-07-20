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
_LAST_RE = re.compile(r"(?:最后|末尾|最末)\s*(?:一)?\s*(?:个|款|件|种)?")


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

    if _LAST_RE.search(text) and (hit_count - 1) not in indices:
        indices.append(hit_count - 1)

    if not indices:
        for match in _BARE_NUM_RE.finditer(text):
            n = _to_int(match.group(1))
            if n and 1 <= n <= hit_count and (n - 1) not in indices:
                indices.append(n - 1)

    return indices


# Distinctive title/brand tokens: Latin model or brand names (FreeBuds/Osprey/iPhone)
# and Chinese runs of at least two characters. These map references back to products.
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")


def _name_tokens(text: str) -> list[str]:
    return _NAME_TOKEN_RE.findall(text or "")


def resolve_by_name(query: str, hits: list[dict]) -> list[int]:
    """Map brand/name references to zero-based indices in ``hits``.

    A hit matches when one distinctive title or brand token occurs in the query. The
    result may contain zero, one, or several indices; callers decide whether to act or
    ask for clarification, so this function favors recall over uniqueness.
    """
    text = query or ""
    lowered = text.lower()
    matches: list[int] = []
    for i, hit in enumerate(hits):
        tokens = _name_tokens(hit.get("title", "")) + _name_tokens(hit.get("brand", ""))
        for tok in tokens:
            if tok.isascii():
                hit_in = tok.lower() in lowered
            else:
                hit_in = tok in text
            if hit_in:
                matches.append(i)
                break
    return matches


# Remove action words, quantifiers, and particles from cart utterances before a
# catalog-wide name search, leaving useful brand/category/model terms.
_ACTION_STOPWORDS = (
    "帮我", "麻烦", "请", "我想", "我要", "想要", "需要", "给我", "替我",
    "把", "将", "再", "也", "还", "这个", "那个", "这款", "那款", "这件", "那件",
    "这部", "那部", "这台", "那台", "它", "他", "她", "该商品", "当前商品",
    "刚才介绍的", "刚刚介绍的", "刚介绍的", "刚才说的", "刚刚说的", "刚说的",
    "刚才", "刚刚", "之前", "上面", "上一个", "上一款", "上面那个", "前面",
    "换成", "换个", "换到", "改成", "不要这个", "我想要",
    "加入购物车", "加进购物车", "加到购物车", "放进购物车", "放购物车",
    "加入", "加进来", "加进去", "加到", "放进", "添加", "加购", "购物车", "加",
    "来一件", "来一个", "来一份", "买一件", "买一个", "买", "下单", "结算",
    "一下", "吧", "呀", "啊", "呢", "哦", "嘛", "的", "了", "一件", "一个", "一份",
)


def extract_name_query(query: str) -> str:
    """Extract catalog-search keywords from an add-to-cart utterance.

    Action words, quantifiers, and particles are removed while brand/category/model
    text is retained. If nothing remains, as with a pure pronoun-only reference,
    return an empty string so the caller uses reference resolution instead.
    """
    text = (query or "").strip()
    if not text:
        return ""
    for word in _ACTION_STOPWORDS:
        text = text.replace(word, " ")
    # Collapse whitespace and remove residual punctuation.
    cleaned = re.sub(r"[\s，。、!！?？~]+", " ", text).strip()
    # Ordinal-only references are not product keywords either.
    if not cleaned or _ORDINAL_RE.fullmatch(cleaned) or cleaned in (
        "前", "俩", "两个", "两款", "全部", "所有", "都",
    ):
        return ""
    return cleaned
