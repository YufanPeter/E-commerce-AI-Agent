from __future__ import annotations

"""Lightweight reference parsing for conversational tool targets."""

import re
from i18n import is_english


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


# 相对指代：相对于「当前聚焦的那一款」（product_detail 最近介绍过 / 刚加购的）往前或往后。
_RELATIVE_PREV: tuple[str, ...] = (
    "上一个", "上一款", "上一件", "上一双", "上一台", "上一部",
    "前一个", "前一款", "前一件", "前一双", "前一台", "前一部",
    "上面那个", "上面那款", "前面那个", "前面那款", "再往前", "往前一个",
)
_RELATIVE_NEXT: tuple[str, ...] = (
    "下一个", "下一款", "下一件", "下一双", "下一台", "下一部",
    "后一个", "后一款", "后一件", "后面那个", "后面那款", "下面那个", "下面那款",
    "再往后", "往后一个",
)


def resolve_relative_index(query: str, hit_count: int, focus_index: int | None) -> int | None:
    """「上一个 / 下一个」这类相对指代：基于当前聚焦商品的下标前后挪一位。

    返回 0-based 下标；这句没有相对指代、或没有聚焦商品、或越界时返回 None。
    """
    text = _strip_absolute_last(query)
    if hit_count <= 0:
        return None
    step = 0
    if any(word in text for word in _RELATIVE_PREV):
        step = -1
    elif any(word in text for word in _RELATIVE_NEXT):
        step = 1
    if step == 0 or focus_index is None:
        return None
    target = focus_index + step
    if 0 <= target < hit_count:
        return target
    return None


def _strip_absolute_last(query: str) -> str:
    """「最后一个 / 最末一款」是绝对序号，不是相对指代——先把「最后」「最末」抹掉，
    免得「最后一个」里的「后一个」被当成「下一个」。"""
    return (query or "").replace("最后", "").replace("最末", "").replace("末尾", "")


def has_relative_reference(query: str) -> bool:
    text = _strip_absolute_last(query)
    return any(word in text for word in _RELATIVE_PREV + _RELATIVE_NEXT)


def resolve_indices(query: str, hit_count: int, focus_index: int | None = None) -> list[int]:
    """Parse a user utterance into 0-based indices over recent hits.

    ``focus_index``：当前聚焦商品在 hits 里的下标（没有则 None），用于解析
    「上一个 / 下一个」这类相对指代。没有焦点时相对指代解析为空，交上层反问，
    而不是把「前一个」误当成「前 1 个 = 第一个」。
    """
    if hit_count <= 0:
        return []

    text = query or ""
    if is_english() and not re.search(r"[\u4e00-\u9fff]", text):
        words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
        relative = re.search(r"\b(previous|next)\s+(?:one|item|product)\b", text, re.I)
        if relative and focus_index is not None:
            index = focus_index + (-1 if relative[1].lower() == "previous" else 1)
            return [index] if 0 <= index < hit_count else []
        matches = re.finditer(
            r"\b(first|second|third|fourth|fifth|last)\b|\b(?:option|item|number|product)\s+(\d+)\b|#(\d+)\b|\b(\d+)(?:st|nd|rd|th)\b",
            text, re.I,
        )
        indices = [
            (hit_count if m[1].lower() == "last" else words[m[1].lower()]) - 1 if m[1]
            else int(m[2] or m[3] or m[4]) - 1
            for m in matches
        ]
        if not indices and text.strip().isdigit():
            indices = [int(text.strip()) - 1]
        if not indices and re.search(r"\b(?:both|these two|top two)\b", text, re.I):
            indices = list(range(min(2, hit_count)))
        return list(dict.fromkeys(i for i in indices if 0 <= i < hit_count))
    if any(word in text for word in ("全部", "所有", "都对比", "都看看")):
        return list(range(hit_count))

    if has_relative_reference(text):
        relative = resolve_relative_index(text, hit_count, focus_index)
        return [relative] if relative is not None else []

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


# 品类通用词：出现在很多商品标题里（「...5G手机」「...蓝牙耳机」），用户说「黑色款手机」时
# 「手机」不是在点名某一款。这些词不参与名称匹配。
_GENERIC_NAME_TOKENS: frozenset[str] = frozenset({
    "手机", "耳机", "平板", "电脑", "笔记本", "跑鞋", "鞋子", "衣服", "外套", "裤子",
    "精华", "面霜", "洁面", "防晒", "面膜", "饮料", "零食", "咖啡", "牛奶", "背包", "帽子",
    "商品", "产品", "款式", "推荐", "智能", "无线", "蓝牙", "运动", "男款", "女款", "旗舰",
})


def _name_tokens(text: str) -> list[str]:
    return [tok for tok in _NAME_TOKEN_RE.findall(text or "") if tok not in _GENERIC_NAME_TOKENS]


def _attribute_tokens(hit: dict) -> list[str]:
    """候选商品的可选规格值（颜色 / 尺码 / 容量…），由 ``options`` 字段给出：

        {"颜色": ["黑色", "白色"], "尺码": ["41", "42"]}

    用户说「黑色那个」「42 码的」时靠这些值定位。只取 ≥2 个字符的值，
    避免单字母 / 单数字误命中。"""
    options = hit.get("options") or {}
    tokens: list[str] = []
    if isinstance(options, dict):
        for values in options.values():
            for value in values or []:
                value = str(value).strip()
                if len(value) >= 2 and value not in tokens:
                    tokens.append(value)
    return tokens


from store.sku_select import color_stems_in as _color_stems_in  # noqa: E402 - 色系词根与 SKU 规格选择共用


def attribute_matches(query: str, hit: dict) -> bool:
    """对外：这句话是否点到了该候选的某个规格值（整值或色系）。"""
    return _attribute_matches(query, hit)


def _attribute_matches(query: str, hit: dict) -> bool:
    """规格值指代：整值命中（「42码」「宇宙橙」）或色系词根命中（「黑色」→「星芒黑」）。"""
    options = hit.get("options") or {}
    if not isinstance(options, dict) or not options:
        return False
    text = query or ""
    # "42 码" → "42码"，和库里 canonical 值对齐
    text = re.sub(r"(\d)\s+(码|号|寸|英寸|GB|L)", r"\1\2", text)
    lowered = text.lower()
    for tok in _attribute_tokens(hit):
        if (tok.lower() in lowered) if tok.isascii() else (tok in text):
            return True
    stems = _color_stems_in(text)
    if not stems:
        return False
    for dim, values in options.items():
        if "色" not in str(dim):
            continue
        for value in values or []:
            if any(stem in str(value) for stem in stems):
                return True
    return False


def resolve_by_name(query: str, hits: list[dict]) -> list[int]:
    """按品牌/名称/规格值把用户这句话映射到 hits 里的商品，返回命中的 0-based 索引。

    匹配规则：某个 hit 的 title/brand 里只要有一个显著词（英文型号词或 ≥2 字
    中文片段）出现在 query 中，即视为命中；候选带 ``options``（SKU 规格值）时，
    规格值（「黑色」「42码」）出现在 query 里也算命中——这就是「黑色那个」的来源。
    命中可能为 0/1/多个——调用方据此决定精确加购还是反问，因此这里偏召回、不强求唯一。"""
    text = query or ""
    lowered = text.lower()
    name_matches: list[int] = []
    attr_matches: list[int] = []
    for i, hit in enumerate(hits):
        tokens = _name_tokens(hit.get("title", "")) + _name_tokens(hit.get("brand", ""))
        for tok in tokens:
            if tok.isascii():
                hit_in = tok.lower() in lowered
            else:
                hit_in = tok in text
            if hit_in:
                name_matches.append(i)
                break
        if _attribute_matches(text, hit):
            attr_matches.append(i)
    # 规格值要在候选之间有区分度：四款跑鞋都有 42 码，「42 码的」就不指向任何一款
    # （调用方会退到当前聚焦的商品）；只有部分候选有黑色，「黑色那个」才成立。
    if len(hits) > 1 and len(attr_matches) == len(hits):
        attr_matches = []
    # 名称 / 品牌是强信号，规格值是弱信号：「阿迪达斯 42 码的」里点了名的只有一款。
    # 两者都有就取交集，交集为空以名称为准；没点名才看规格。
    if name_matches and attr_matches:
        both = [i for i in name_matches if i in attr_matches]
        return both or name_matches
    return name_matches or attr_matches


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
