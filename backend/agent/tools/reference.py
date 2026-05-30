from __future__ import annotations

"""指代消解：把用户口语里的"第一个/这俩/那个珀莱雅的"映射回 last_hits 的下标。

compare 和 product_detail 共用——它们都要先搞清楚"用户到底在说上一轮的哪几款"，
再去 ProductStore 拉全貌。把这块逻辑抽出来集中维护，避免两个 tool 各写一份正则。

纯字符串/正则，无 LLM、无 IO：定位是确定性问题，上 LLM 既慢又会幻觉出
越界下标。命中不了就返回空，由调用方决定默认值（compare 默认前两个）或追问。
"""

import re


# 中文数字 → 阿拉伯数字（够覆盖 last_hits 量级，一般 ≤5 个）
_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# "第一个" / "第 2 款" / "第3个" / "第一" ...
_ORDINAL_RE = re.compile(r"第\s*([0-9]+|[一二两三四五六七八九十])\s*(?:个|款|件|种)?")
# 裸数字序号 "1和3" / "2、3"
_BARE_NUM_RE = re.compile(r"(?<!\d)([1-9])(?!\d)")


def _to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)


def resolve_indices(query: str, hit_count: int) -> list[int]:
    """把 query 解析成 last_hits 的 0-based 下标列表（去重、保序、越界丢弃）。

    支持：
        "第一个"/"第1款"        → [0]
        "第一个和第三个"/"1和3"  → [0, 2]
        "前两个"/"这俩"/"两个"   → [0, 1]
        "全部"/"都"             → 全部
    无法识别时返回 []，由调用方兜底。
    """
    if hit_count <= 0:
        return []

    text = query or ""

    # "全部" / "所有" / "都"
    if any(w in text for w in ("全部", "所有", "都对比", "都看看")):
        return list(range(hit_count))

    # "前两个" / "前 3 个"
    front = re.search(r"前\s*([0-9]+|[一二两三四五六七八九十])", text)
    if front:
        n = _to_int(front.group(1)) or 0
        return list(range(min(n, hit_count)))

    # "这俩" / "两个" / "这两款" → 前两个
    if any(w in text for w in ("这俩", "俩", "两个", "两款", "二者")):
        return list(range(min(2, hit_count)))

    indices: list[int] = []

    # 优先匹配"第 X 个"这类显式序数
    for m in _ORDINAL_RE.finditer(text):
        n = _to_int(m.group(1))
        if n and 1 <= n <= hit_count and (n - 1) not in indices:
            indices.append(n - 1)

    # 没有"第"字时，退回裸数字序号（"1和3"）
    if not indices:
        for m in _BARE_NUM_RE.finditer(text):
            n = _to_int(m.group(1))
            if n and 1 <= n <= hit_count and (n - 1) not in indices:
                indices.append(n - 1)

    return indices


def resolve_by_title(query: str, hits: list[dict]) -> int | None:
    """用 query 里的关键词去匹配 last_hits 的 title/brand（"那个珀莱雅的"）。

    返回第一个命中的下标；无命中返回 None。只在序数定位失败后兜底用。
    """
    text = query or ""
    for idx, hit in enumerate(hits):
        title = str(hit.get("title", ""))
        # 标题里任意 2 字以上的连续片段出现在 query 中即算命中（粗匹配够用）
        for token in _meaningful_tokens(title):
            if token in text:
                return idx
    return None


def _meaningful_tokens(title: str) -> list[str]:
    """从标题里切出可用于匹配的片段：英文整词 + 中文 3-gram。

    为什么用 3-gram 而非整段中文：品牌名常是 3 字（珀莱雅/欧莱雅），整段
    run（"欧莱雅面霜"）几乎不可能原样出现在用户口语里；而 2-gram 又太短，
    "莱雅"会让"珀莱雅"和"欧莱雅"互相误命中。3-gram 在区分度和召回间最平衡。
    """
    tokens: list[str] = re.findall(r"[A-Za-z]{2,}", title)
    for run in re.findall(r"[一-鿿]{2,}", title):
        if len(run) <= 3:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 3] for i in range(len(run) - 2))
    # 长的优先（更具区分度），去重
    return sorted(set(tokens), key=len, reverse=True)
