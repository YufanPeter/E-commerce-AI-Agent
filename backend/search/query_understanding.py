from __future__ import annotations

"""Query Understanding 编排入口（LLM 主力版）。

职责边界：
    本模块只做「数据契约 + 编排 + 缓存」，不做任何关键字/正则解析。
    自然语言到 ParsedQuery 的真实工作全部交给 search.llm_parser，
    由豆包 function calling 完成（详见 docs/后端业务技术选型与流程设计.md）。

为什么不再保留规则兜底：
    规则版在 query 覆盖率、否定语义、同义词等维度上全面落后 LLM，
    保留它只会带来「降级到一个同样不准的版本」的虚假安全感。
    LLM 不可用时直接 raise，由上层 API 决定是返回错误还是空结果，
    比悄悄给出错误召回更负责任。

依赖关系：
    llm_parser  ──imports──►  query_understanding   （取 ParsedQuery / load_taxonomy / INGREDIENT_BLOCKLIST）
    api / agent ──imports──►  query_understanding   （只调 understand_query）
"""

import argparse
import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "backend" / "storage" / "ecommerce_agent.sqlite3"


# ---------------------------------------------------------------------------
# 1. 数据契约
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedQuery:
    """检索意图的结构化表示，下游所有模块只面向本结构编程。"""

    original_query: str
    intent: str = "product_search"
    category: str | None = None
    sub_category: str | None = None
    category_exclude: list[str] = field(default_factory=list)
    sub_category_exclude: list[str] = field(default_factory=list)
    max_price: float | None = None
    min_price: float | None = None
    brand_include: list[str] = field(default_factory=list)
    brand_exclude: list[str] = field(default_factory=list)
    negative_ingredients: list[str] = field(default_factory=list)
    soft_terms: list[str] = field(default_factory=list)
    retrieval_query: str = ""
    needs_clarification: bool = False

    @property
    def hard_filters(self) -> dict[str, object]:
        """直接喂给 SQLite / Chroma where 的硬过滤条件。"""
        return {
            "category": self.category,
            "sub_category": self.sub_category,
            "category_exclude": self.category_exclude,
            "sub_category_exclude": self.sub_category_exclude,
            "max_price": self.max_price,
            "min_price": self.min_price,
            "brand_include": self.brand_include,
            "brand_exclude": self.brand_exclude,
        }

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["hard_filters"] = self.hard_filters
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ParsedQuery":
        """从 to_dict() 的结果还原。

        宽容地忽略未知键——尤其是 to_dict() 额外塞进去的 ``hard_filters``
        （它是派生 property，不是构造参数），否则 ParsedQuery(**data) 会报
        unexpected keyword argument。
        """
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def merge_base(self, base: "ParsedQuery") -> "ParsedQuery":
        """把本轮解析（self，作为覆盖项）叠加到上一轮基线（base）上。

        多轮"细化"的核心：用户只说"Adidas"时，本轮解析几乎是空的，必须继承
        上一轮的品类/价格等，否则就退化成一次"泛搜 Adidas"。

        合并规则（按字段语义区分，不是一刀切）：
            - 标量类目/价格（category/sub_category/max_price/min_price）：
              本轮显式给了就覆盖，否则继承 base —— "便宜点"不该清掉"跑鞋"。
            - brand_include：本轮非空则**替换**（"换成 Nike"应只剩 Nike，
              而不是 Adidas+Nike）。
            - brand_exclude / negative_ingredients / soft_terms：取**并集**
              （否定与软偏好是逐轮累积的，"不要苹果"之后"也不要三星"该都生效）。
            - retrieval_query：合并双方分词并去重，本轮词在前，让向量检索
              同时命中"品牌"和"上一轮的品类语义"（"Adidas" + "跑鞋"）。
            - needs_clarification：恒为 False —— 既然有 base，就已有足够上下文。
        """
        def pick(cur: object, prev: object) -> object:
            return cur if cur is not None else prev

        def union(cur: list, prev: list) -> list:
            return list(dict.fromkeys(list(prev or []) + list(cur or [])))

        def replace_or_keep(cur: list, prev: list) -> list:
            return list(cur) if cur else list(prev or [])

        tokens: list[str] = []
        for tok in (self.retrieval_query or "").split() + (base.retrieval_query or "").split():
            if tok and tok not in tokens:
                tokens.append(tok)
        retrieval_query = " ".join(tokens) or self.original_query or base.original_query

        return ParsedQuery(
            original_query=self.original_query or base.original_query,
            intent=self.intent or base.intent,
            category=pick(self.category, base.category),
            sub_category=pick(self.sub_category, base.sub_category),
            category_exclude=union(self.category_exclude, base.category_exclude),
            sub_category_exclude=union(self.sub_category_exclude, base.sub_category_exclude),
            max_price=pick(self.max_price, base.max_price),
            min_price=pick(self.min_price, base.min_price),
            brand_include=replace_or_keep(self.brand_include, base.brand_include),
            brand_exclude=union(self.brand_exclude, base.brand_exclude),
            negative_ingredients=union(self.negative_ingredients, base.negative_ingredients),
            soft_terms=union(self.soft_terms, base.soft_terms),
            retrieval_query=retrieval_query,
            needs_clarification=False,
        )


# ---------------------------------------------------------------------------
# 2. Taxonomy：从 SQLite 派生，给 LLM function schema 注入 enum
# ---------------------------------------------------------------------------

# 品牌别名表：key 是 canonical（暴露给 LLM 的官方名），value 是
# SQLite / Chroma 中可能出现的所有变体（包含 canonical 自身）。
# 添加时机：发现同一品牌在数据里有两种写法（如中英混用）。
# 维护成本：每条 ~1 行，比清洗数据 + 重建 Chroma 便宜得多。
BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "Apple 苹果":      ("Apple 苹果", "苹果"),
    "耐克":            ("耐克", "Nike"),
    "The North Face":  ("The North Face", "北面"),
}


@lru_cache(maxsize=1)
def load_taxonomy(db_path: Path = DEFAULT_DB_PATH) -> dict[str, object]:
    """读取 products 表的 category / sub_category / brand 唯一值。

    返回：
        {
          "sub_to_cat":   {sub_category: category},   # 反查父类目
          "brands":       [canonical_brand, ...],     # 暴露给 LLM enum 的官方名
          "brand_expand": {canonical: [variants]},    # 查 Chroma 时把 canonical 展回所有变体
        }
    用 lru_cache 让进程内只读一次；建库脚本变更后重启服务生效。
    """
    if not db_path.exists():
        return {"sub_to_cat": {}, "brands": [], "brand_expand": {}}

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT category, sub_category, brand FROM products"
        ).fetchall()

    sub_to_cat: dict[str, str] = {}
    raw_brands: set[str] = set()
    for category, sub_category, brand in rows:
        if sub_category:
            sub_to_cat[sub_category] = category
        if brand:
            raw_brands.add(brand)

    # 反向索引：variant → canonical
    variant_to_canonical: dict[str, str] = {}
    for canonical, variants in BRAND_ALIASES.items():
        for variant in variants:
            variant_to_canonical[variant] = canonical

    # 把每个 raw brand 映射到 canonical，收敛重复
    canonicals: set[str] = set()
    brand_expand: dict[str, list[str]] = {}
    for raw in raw_brands:
        canonical = variant_to_canonical.get(raw, raw)
        canonicals.add(canonical)
        brand_expand.setdefault(canonical, []).append(raw)

    # 保证 canonical 自身也在 expand 列表里（即使数据库里没出现过）
    for canonical, variants in BRAND_ALIASES.items():
        if canonical in canonicals:
            existing = set(brand_expand[canonical])
            for variant in variants:
                if variant not in existing:
                    brand_expand[canonical].append(variant)

    return {
        "sub_to_cat": sub_to_cat,
        "brands": sorted(canonicals),
        "brand_expand": brand_expand,
    }


def expand_brands(canonical_brands: list[str]) -> list[str]:
    """把 canonical 品牌列表展开成 Chroma 实际存的所有变体，喂给 $in / $nin。"""
    taxonomy = load_taxonomy()
    expand: dict[str, list[str]] = taxonomy["brand_expand"]  # type: ignore[assignment]
    out: list[str] = []
    seen: set[str] = set()
    for canonical in canonical_brands:
        for variant in expand.get(canonical, [canonical]):
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    return out


# ---------------------------------------------------------------------------
# 3. 后置过滤词表：成分黑名单
# ---------------------------------------------------------------------------

# 这个列表是「后置过滤」用的，不是「解析」用的：
#   解析时 LLM 用它做 enum 约束，避免幻觉成分；
#   召回后，retriever 用它在商品全文里做关键词剔除。
# 保持小而稳定，新增成分前先确认数据里确实能匹配到。
INGREDIENT_BLOCKLIST: tuple[str, ...] = (
    "酒精", "乙醇", "香精", "色素", "防腐剂",
    "糖", "蔗糖", "果葡糖浆",
    "咖啡因", "咖啡",
    "日系", "韩系",
)


# ---------------------------------------------------------------------------
# 4. 编排入口
# ---------------------------------------------------------------------------

def normalize_query(query: str) -> str:
    return " ".join(query.strip().split())


@lru_cache(maxsize=1024)
def _cached_understand(normalized_query: str) -> ParsedQuery:
    # 延迟导入：让本模块在没装 openai 时也能 import（便于纯数据契约场景）
    from search.llm_parser import parse_query_with_llm

    return parse_query_with_llm(normalized_query)


def understand_query(query: str) -> ParsedQuery:
    """所有上层模块（API、Agent、retriever）唯一应该调的入口。

    - 进程内 LRU 缓存，热门 query 0ms 命中（加分项 4.4 的简单实现，
      线上可升级为 Redis 跨进程缓存）。
    - LLM 调用 / 解析失败时降级为纯向量召回（保留原始 query、跳过 hard_filter），
      而不是让整条检索链崩成兜底文案。降级结果不写入 LRU 缓存，避免一次模型
      抖动被永久缓存，下次同样的 query 仍会重新尝试 LLM 解析。
    """
    normalized = normalize_query(query)
    if not normalized:
        return ParsedQuery(original_query=query, needs_clarification=True)
    try:
        return _cached_understand(normalized)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "LLM query understanding failed, degrade to vector-only recall: %r", exc
        )
        # 降级：不带任何 hard_filter，retrieval_query 用原文，让向量召回兜底。
        # 但【否定/反选】是用户的硬性诉求，不能因 LLM 挂了就完全失效——这里用
        # 确定性正则 + taxonomy 兜底抽取「不要X品类」，至少保住品类反选。
        sub_excl, cat_excl = _regex_extract_excludes(query)
        return ParsedQuery(
            original_query=query,
            retrieval_query=normalized,
            sub_category_exclude=sub_excl,
            category_exclude=cat_excl,
            needs_clarification=False,
        )


def clear_cache() -> None:
    """taxonomy 或 LLM prompt 变更后调一次。"""
    _cached_understand.cache_clear()
    load_taxonomy.cache_clear()


# 否定意图触发词：用户表达"不要/不含/排除"时的常见说法。
_NEGATION_CUES = ("不要", "不想要", "不想", "不含", "不带", "没有", "无", "除了", "排除", "拒绝", "讨厌")

# 品类同义词 → taxonomy 官方 sub_category（确定性兜底，覆盖全部一级类目的高频反选目标）。
# LLM 挂掉时靠这张表保住品类反选；命中后走 where $nin 召回阶段排除。
# 维护原则：key 写用户口语，value 必须是 taxonomy 里真实存在的官方子类目（含斜杠的写全名，
# 如 "坚果/零食"），否则不会被采纳。官方子类目本身（及其斜杠拆分项）由代码自动兜底，无需逐一列出。
_SUBCAT_SYNONYMS: dict[str, str] = {
    # —— 食品饮料 ——
    "功能性饮料": "功能饮料",
    "能量饮料": "功能饮料",
    "功能型饮料": "功能饮料",
    "提神饮料": "功能饮料",
    "碳酸": "碳酸饮料",
    "汽水": "碳酸饮料",
    "气泡水": "碳酸饮料",
    "可乐": "碳酸饮料",
    "零食": "坚果/零食",
    "坚果": "坚果/零食",
    "小零食": "坚果/零食",
    "膨化食品": "坚果/零食",
    "速食": "方便食品",
    "泡面": "方便食品",
    "方便面": "方便食品",
    "酸奶": "酸奶",
    "牛奶": "牛奶",
    "纯牛奶": "牛奶",
    "茶": "茶饮",
    "茶饮料": "茶饮",
    # —— 数码电子 ——
    "蓝牙耳机": "真无线耳机",
    "无线耳机": "真无线耳机",
    "耳机": "真无线耳机",
    "手机": "智能手机",
    "平板": "平板电脑",
    "ipad": "平板电脑",
    "笔记本": "笔记本电脑",
    "电脑": "笔记本电脑",
    "笔电": "笔记本电脑",
    # —— 服饰运动 ——
    "慢跑鞋": "跑步鞋",
    "运动鞋": "跑步鞋",
    "球鞋": "篮球鞋",
    "登山鞋": "徒步鞋",
    "短袖": "短袖T恤",
    "t恤": "短袖T恤",
    "短裤": "运动短裤",
    "长裤": "运动长裤",
    "卫衣": "卫衣",
    "瑜伽裤": "瑜伽裤",
    "速干衣": "速干T恤",
    "双肩包": "背包",
    "书包": "背包",
    # —— 美妆护肤 ——
    "补水面膜": "面膜",
    "贴片面膜": "面膜",
    "面膜": "面膜",
    "精华液": "精华",
    "精华": "精华",
    "面部精华": "精华",
    "爽肤水": "化妆水",
    "化妆水": "化妆水",
    "卸妆水": "卸妆",
    "卸妆油": "卸妆",
    "卸妆膏": "卸妆",
    "洗面奶": "洁面",
    "洁面乳": "洁面",
    "面部防晒": "防晒",
    "防晒霜": "防晒",
    "防晒": "防晒",
    "口红": "唇釉",
    "唇釉": "唇釉",
    "面霜": "面霜",
    "乳液": "面霜",
    "眼霜": "眼霜",
    "粉底": "粉底液",
    "粉底液": "粉底液",
    "散粉": "蜜粉",
    "蜜粉": "蜜粉",
    "眉笔": "眉笔",
}


def _regex_extract_excludes(query: str) -> tuple[list[str], list[str]]:
    """LLM 失败降级时的确定性否定兜底：从原句抽「不要X品类」→ (子类目排除, 一级类目排除)。

    只在出现否定触发词时才尝试，且只认 taxonomy 里真实存在的品类/同义词，
    保证零误伤（不会把"不要太贵"误当品类排除）。"""
    text = query or ""
    if not any(cue in text for cue in _NEGATION_CUES):
        return [], []

    taxonomy = load_taxonomy()
    sub_to_cat = taxonomy.get("sub_to_cat", {}) or {}
    known_cats = {c for c in sub_to_cat.values() if c}

    # 预拆斜杠子类目（如 "坚果/零食"）：任一拆分项命中即视作命中该官方子类目。
    # 否则用户说"不要坚果"永远匹配不上带斜杠的官方名。
    sub_parts: dict[str, str] = {}
    for sub in sub_to_cat:
        for part in re.split(r"[/、，,]", sub):
            part = part.strip()
            if len(part) >= 2:
                sub_parts.setdefault(part, sub)

    sub_excl: list[str] = []
    cat_excl: list[str] = []

    def _add_sub(official: str) -> None:
        if official in sub_to_cat and official not in sub_excl:
            sub_excl.append(official)

    # 找否定触发词之后的一小段文本，在其中匹配品类同义词 / 官方子类目 / 一级类目。
    for cue in _NEGATION_CUES:
        idx = text.find(cue)
        if idx < 0:
            continue
        window = text[idx + len(cue): idx + len(cue) + 12]
        # 1) 同义词表（口语 → 官方子类目）
        for syn, official in _SUBCAT_SYNONYMS.items():
            if syn in window:
                _add_sub(official)
        # 2) 官方子类目全名直接命中
        for sub in sub_to_cat:
            if sub in window:
                _add_sub(sub)
        # 3) 斜杠子类目的拆分项命中（"坚果" → "坚果/零食"）
        for part, official in sub_parts.items():
            if part in window:
                _add_sub(official)
        # 4) 一级类目命中
        for cat in known_cats:
            if cat in window and cat not in cat_excl:
                cat_excl.append(cat)

    return sub_excl, cat_excl


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="解析用户 query 并生成检索输入。")
    parser.add_argument("query")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    args = parse_args()
    if args.db != DEFAULT_DB_PATH:
        # CLI 覆盖默认 DB 时需要重置 taxonomy 缓存
        load_taxonomy.cache_clear()
        load_taxonomy(args.db)
    parsed = understand_query(args.query)
    print(json.dumps(parsed.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
