from __future__ import annotations

"""CompareTool：对比上一轮命中的 2-3 个商品。

触发场景：
    - "这两个有什么区别"
    - "A 和 B 哪个适合干皮"
    - "对比一下第一个和第三个"
    - 主动型："帮我对比下"（默认取 last_hits 前 2 个）

设计意图：
    把对比拆成"定位 + 结构化抽取"两步：
        ① 用 reference 模块把"第一个和第三个"/"华为和小米"映射回具体商品；
        ② 用 agent.comparison.build_comparison 产出干净的维度表 + 购买建议。
    iOS 端拿到的是可直接渲染的 comparison 结构（products + rows + recommendation），
    而不是一段自由发挥的文本。

依赖：
    - ProductStore.get_product_detail：取每个商品的全貌（spec/faq/review）
    - session.recall_hits()：上一轮命中商品，供指代定位
"""

import logging
import re
from typing import Any

from agent.comparison import build_comparison
from agent.session import AgentSession
from agent.tools.base import ToolResult
from agent.tools.reference import resolve_by_name, resolve_indices
from store.product_store import ProductStore


logger = logging.getLogger(__name__)

# 固定对比 2 个商品（对比表两列最清晰，也是用户预期）。
_MAX_COMPARE = 2

_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")
_GENERIC_NAME_TOKENS = {
    "pro", "max", "plus", "ultra", "mini", "air", "gb", "tb", "手机", "商品",
    "旗舰", "推荐", "列表", "差异", "区别", "对比", "比较", "两个", "两款",
    "这两个", "这两款", "哪款", "哪个",
}


class CompareTool:
    name: str = "compare"

    def __init__(self, product_store: ProductStore | None = None) -> None:
        self._store = product_store or ProductStore()

    def run(
        self,
        query: str,
        session: AgentSession,
        slots: dict[str, Any],
    ) -> ToolResult:
        last_hits = session.recall_hits()
        if len(last_hits) < 2:
            return self._plain(
                "还没有可对比的商品哦，先让我帮你推荐几款，"
                "然后说「对比一下第一个和第二个」就可以。"
            )

        # 前端对比页可能直接指定商品 id（绕过指代解析）。
        explicit_ids = slots.get("product_ids")
        if explicit_ids:
            product_ids = [pid for pid in explicit_ids][:_MAX_COMPARE]
        else:
            product_ids = self._resolve_targets(query, last_hits)

        if len(product_ids) < 2:
            # 定位不到 2 款 → 反问。记下「正在对比追问」，下一轮用户回答
            # 「第一个和第三个」时 orchestrator 才能把它强制送回 compare，
            # 而不是被 router 误判成 product_detail/refine。候选就是 last_hits
            # 全集，所以无需另存子集，只需标记待定态。
            session.set("pending_compare", {"hit_count": len(last_hits)})
            return self._plain(
                "想对比哪几款呢？可以说「对比第一个和第三个」，"
                "或者「对比华为和小米那两款」。"
            )

        # 定位成功 → 清掉「对比追问」待定态，避免粘住下一轮。
        session.set("pending_compare", None)

        details = []
        for pid in product_ids:
            detail = self._store.get_product_detail(pid)
            if detail is not None:
                details.append(detail)
        if len(details) < 2:
            return self._plain("这些商品的资料不太全，换两款再试试对比？")

        focus = slots.get("focus") or query
        try:
            comparison = build_comparison(details, focus=focus)
        except Exception:  # noqa: BLE001 - 兜底，绝不让对比把整个 turn 打挂
            logger.exception("build_comparison failed")
            return self._plain("对比生成出了点问题，稍后再试或换两款商品？")

        # 把参与对比的商品回写工作记忆，便于后续"第一个加入购物车"等接续指代。
        session.set(
            "last_hits",
            [
                {"product_id": p["product_id"], "title": p["title"]}
                for p in comparison["products"]
            ],
        )

        # 对话里给一句中性引导（不再给选购建议），让用户看下面的对比表自行判断。
        titles = "」「".join(p["title"][:14] for p in comparison["products"])
        narrative = f"已为你对比「{titles}」，下面是各维度对照，可按自己看重的方面来选～"

        return ToolResult(
            tool_name=self.name,
            payload={"action": "compare", "comparison": comparison},
            narrative_override=narrative,
            needs_composer=False,
        )

    def _resolve_targets(
        self, query: str, last_hits: list[dict[str, Any]]
    ) -> list[str]:
        """把用户这句话定位到要对比的商品 id 列表（保序去重，最多 _MAX_COMPARE）。

        优先级：品牌/名称点名（"华为和小米这两款"）优先于泛指"这两款"；
        否则再按显式序号（"第一个和第三个"）解析；都没说时默认前两个。"""
        strict_named = _resolve_context_names(query, last_hits)
        if len(strict_named) >= 2:
            indices = strict_named
        else:
            named = strict_named or resolve_by_name(query, last_hits)
            # 若用户已经点名了某些商品，"这两款/两个"只是语气里的泛指，不能再把
            # 目标兜底成前两个；否则 "小米和华为这两款" 在小米不在 last_hits 时
            # 会错误对比前两个 Apple。先剥掉泛指词，只保留真正的序号解析。
            index_query = _strip_generic_pair_words(query) if named else query
            indices = resolve_indices(index_query, len(last_hits))
            if len(indices) < 2:
                for i in named:  # 合并序号与名称命中，保序去重
                    if i not in indices:
                        indices.append(i)
        if len(indices) < 2 and not (strict_named or resolve_by_name(query, last_hits)):
            indices = list(range(min(2, len(last_hits))))  # 没点名 → 默认前两个

        ordered: list[str] = []
        for i in indices[:_MAX_COMPARE]:
            pid = last_hits[i]["product_id"]
            if pid not in ordered:
                ordered.append(pid)
        return ordered

    def _plain(self, text: str) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            payload={"action": "compare", "comparison": None},
            narrative_override=text,
            needs_composer=False,
        )


def _resolve_context_names(query: str, hits: list[dict[str, Any]]) -> list[int]:
    """在上一轮 hits 内按“明确品牌/型号词”匹配用户点名的两款。

    ``reference.resolve_by_name`` 偏召回，会把 ``Pro`` 这类通用型号词命中到
    iPhone Pro。对 compare 来说，用户说“小米和华为这两款”时，我们更需要
    保守地按上下文标题里的品牌/型号词定位，并忽略泛词。"""
    text = query or ""
    lowered = text.lower()
    mentions: list[tuple[int, int, int]] = []  # (出现位置, -词长度, hit index)

    for i, hit in enumerate(hits):
        tokens = _significant_tokens(hit.get("title", ""))
        tokens.extend(_significant_tokens(hit.get("brand", "")))
        seen: set[str] = set()
        for token in tokens:
            key = token.lower() if token.isascii() else token
            if key in seen:
                continue
            seen.add(key)
            pos = lowered.find(key) if token.isascii() else text.find(token)
            if pos >= 0:
                mentions.append((pos, -len(token), i))
                break

    mentions.sort()
    indices: list[int] = []
    for _, _, i in mentions:
        if i not in indices:
            indices.append(i)
    return indices


def _significant_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _NAME_TOKEN_RE.findall(text or ""):
        key = token.lower() if token.isascii() else token
        if key in _GENERIC_NAME_TOKENS:
            continue
        tokens.append(token)
    return tokens


def _strip_generic_pair_words(query: str) -> str:
    text = query or ""
    for word in ("这两款", "这两个", "那两款", "那两个", "两款", "两个", "这俩", "俩", "二者"):
        text = text.replace(word, " ")
    return text


# 明显的「开新检索/换品类」信号——出现这些词时，pending_compare 不再粘住。
_COMPARE_NEW_SEARCH_WORDS: tuple[str, ...] = (
    "推荐", "找", "有哪些", "有什么", "想买", "给我", "来几款", "换个", "换成", "看看别的",
    "加入购物车", "加购", "下单", "结算",
)

# 连接两个被点名商品的关系词（"第一个和第三个" / "华为跟小米"）。
_COMPARE_LINK_WORDS: tuple[str, ...] = ("和", "跟", "与", "还有", "以及", "对比", "比较", "vs", "VS")


def is_compare_selection_reply(query: str, hit_count: int) -> bool:
    """判断这句是否像在回答「想对比哪几款」，用于 pending_compare 的粘性判定。

    命中条件：解析出 ≥2 个序号、命中 ≥2 个商品名、或含连接词（和/跟/对比）。
    出现明显的开新检索词（推荐/找/加购…）则判否，让待定态及时释放。"""
    text = (query or "").strip()
    if not text:
        return False
    if any(word in text for word in _COMPARE_NEW_SEARCH_WORDS):
        return False
    if len(resolve_indices(text, max(hit_count, 1))) >= 2:
        return True
    if any(word in text for word in _COMPARE_LINK_WORDS):
        return True
    return False


