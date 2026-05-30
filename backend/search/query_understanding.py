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
import sqlite3
from dataclasses import asdict, dataclass, field
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
            "max_price": self.max_price,
            "min_price": self.min_price,
            "brand_include": self.brand_include,
            "brand_exclude": self.brand_exclude,
        }

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["hard_filters"] = self.hard_filters
        return data


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
        return ParsedQuery(
            original_query=query,
            retrieval_query=normalized,
            needs_clarification=False,
        )


def clear_cache() -> None:
    """taxonomy 或 LLM prompt 变更后调一次。"""
    _cached_understand.cache_clear()
    load_taxonomy.cache_clear()


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
