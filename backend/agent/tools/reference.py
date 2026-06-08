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


# title/brand 里有区分度的词：英文型号/品牌词（FreeBuds/Osprey/iPhone）
# 与 ≥2 字的中文连续片段（华为/北面/双肩背包）。用于把"华为的那个"映射回具体商品。
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")


def _name_tokens(text: str) -> list[str]:
    return _NAME_TOKEN_RE.findall(text or "")


def resolve_by_name(query: str, hits: list[dict]) -> list[int]:
    """按品牌/名称把用户这句话映射到 hits 里的商品，返回命中的 0-based 索引。

    匹配规则：某个 hit 的 title/brand 里只要有一个显著词（英文型号词或 ≥2 字
    中文片段）出现在 query 中，即视为命中。命中可能为 0/1/多个——调用方据此
    决定精确加购还是反问，因此这里偏召回、不强求唯一。"""
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


# 加购/管车话里的动作词、量词、语气词——做全库点名检索前先剥掉，
# 留下真正的商品关键词（品牌/品类/型号），如"把小米加进来"→"小米"。
_ACTION_STOPWORDS = (
    "帮我", "麻烦", "请", "我想", "我要", "想要", "需要", "给我", "替我",
    "把", "将", "再", "也", "还", "这个", "那个", "这款", "那款", "这件", "那件",
    "刚才", "刚刚", "之前", "上面", "上一个", "上一款", "上面那个", "前面",
    "换成", "换个", "换到", "改成", "不要这个", "我想要",
    "加入购物车", "加进购物车", "加到购物车", "放进购物车", "放购物车",
    "加入", "加进来", "加进去", "加到", "放进", "添加", "加购", "购物车", "加",
    "来一件", "来一个", "来一份", "买一件", "买一个", "买", "下单", "结算",
    "一下", "吧", "呀", "啊", "呢", "哦", "嘛", "的", "了", "一件", "一个", "一份",
)


def extract_name_query(query: str) -> str:
    """从加购话里抽出用于全库检索的商品关键词。

    策略：剥掉动作/量词/语气停用词后，保留剩余的品牌/品类/型号文本。
    例："把小米加进来"→"小米"、"帮我加一个 OPPO Reno"→"OPPO Reno"、
    "再来一件北面冲锋衣"→"北面冲锋衣"。剥光了（纯指代如"这个"）则返回空串，
    交由调用方走指代消解而非全库检索。"""
    text = (query or "").strip()
    if not text:
        return ""
    for word in _ACTION_STOPWORDS:
        text = text.replace(word, " ")
    # 折叠空白、去掉残留标点
    cleaned = re.sub(r"[\s，。、!！?？~]+", " ", text).strip()
    # 只剩序号指代（"第二个"/"前两个"/"这俩"）也不是商品关键词，视为空。
    if not cleaned or _ORDINAL_RE.fullmatch(cleaned) or cleaned in (
        "前", "俩", "两个", "两款", "全部", "所有", "都",
    ):
        return ""
    return cleaned
