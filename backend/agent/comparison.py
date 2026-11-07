from __future__ import annotations

"""商品对比的核心构建逻辑（对话 tool 与 REST /compare 端点共用）。

产出一个干净、可直接渲染成对比表格的结构：
    {
      "title": "对比：A vs B",
      "products": [{product_id,title,brand,price,image_url,
                    sku:{sku_id,label,options,price,price_display}, skus:[...可切换规格], price_range}, ...],
      "rows": [{"label": "价格", "values": ["¥8999","¥5999"], "highlight": 1}, ...],
      "recommendation": "什么人选哪个的一句话建议"
    }

【SKU 级对比】对比的是两件商品各自的【一个确定规格】，而不是整个价格区间：
- 规格怎么定：显式 sku_id → 用户话里点到的规格（「256G 的」）→ 预算内最便宜 → 最便宜。
  两边用同一套规则，口径一致。
- 价格行用选中规格的确定价格；再给一行「价格区间」看全貌。
- 规格差异按维度拆成独立行（存储 / 颜色 / 尺码…），值取自选中规格；
  两边都一样的维度折叠不显示。
- 表头带 skus 列表，前端可点列切换规格；对话里说「换成 512G 的」走 compare 的
  规格切换路径，只换那一列重算。

【设计：离线预抽取 + 运行时纯查表，运行时零 LLM】
- 维度值（续航/性能/成分/材质…）藏在商品的非结构化文本里（marketing_description /
  official_faq），不是独立字段。我们用 build_compare_index 脚本【离线】把每个商品的
  固定维度值 + 一句"适合人群"预抽取好，落盘到 compare_index.json。
- 运行时 build_comparison 只做【纯查表】：价格行用真实数据确定性生成，其余维度行
  从索引里按商品 id 查值拼成两列。**不调用任何 LLM**——快、稳、可复现、零成本。
- 维度名由代码定死（_CATEGORY_DIMENSIONS），同类目商品永远用同一套维度对比；
  文本型维度不强行判优（highlight=None），把判断交给用户，只有价格确定性高亮。

索引缺失（没跑过离线脚本）时优雅降级：只剩价格行 + 基于价格的兜底建议。
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from store.product_store import ProductDetail, price_display
from store.sku_select import all_dims, select_sku, sku_label, sku_to_dict


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_IMAGE_MANIFEST_PATH = PROJECT_ROOT / "backend" / "cdn" / "image_manifest.json"
COMPARE_INDEX_PATH = PROJECT_ROOT / "backend" / "storage" / "compare_index.json"


def _load_image_manifest() -> dict[str, str]:
    if not _IMAGE_MANIFEST_PATH.exists():
        return {}
    with _IMAGE_MANIFEST_PATH.open("r", encoding="utf-8") as f:
        parsed = json.load(f)
    return {str(k): str(v) for k, v in parsed.items() if k and v}


_IMAGE_MANIFEST = _load_image_manifest()


# 各类目【固定】的对比维度（有序）。维度名由代码定死，保证同类目商品每次都用
# 同一套维度对比——稳定、可复现、公平。价格不在此列（由确定性 _price_row 生成）。
#
# 这套维度是【对照真实数据】定的：每个维度在该类目的 marketing_description /
# official_faq / skus.properties 里都有内容可填，不会出现整行空白。
#   - 数码：芯片性能、续航、屏幕在 desc/FAQ 明确写到；存储来自 properties
#   - 美妆：功效、成分、适用肤质在 desc 写到；容量来自 properties
#   - 服饰：面料、版型、场景在 desc 写到；尺码/颜色来自 properties
#   - 食品：风味、原料、无添加在 desc 写到；口味/数量来自 properties
_CATEGORY_DIMENSIONS: dict[str, list[str]] = {
    "数码电子": ["性能/芯片", "续航", "屏幕", "存储规格"],
    "美妆护肤": ["核心功效", "关键成分", "适用肤质", "容量规格"],
    "服饰运动": ["材质面料", "版型", "适用场景", "尺码颜色"],
    "食品生活": ["口味风味", "配料成分", "规格分量", "适用场景"],
}

# 跨类目（商品类目不一致）时用的通用维度。
_GENERIC_DIMENSIONS = ["品牌定位", "核心卖点", "适用场景"]

# 缺值占位符。
_EMPTY = "—"


def _dimensions_for(details: list[ProductDetail]) -> list[str]:
    """根据参与对比商品的类目，选定一套【固定】维度。

    同类目 → 该类目专属维度；跨类目 → 通用维度。返回的维度名即最终表格行名。"""
    categories = {d.category for d in details}
    if len(categories) == 1:
        return _CATEGORY_DIMENSIONS.get(next(iter(categories)), _GENERIC_DIMENSIONS)
    return _GENERIC_DIMENSIONS


def all_dimensions_for_category(category: str) -> list[str]:
    """离线抽取时用：某商品需要预抽取的全部维度 = 类目专属 + 通用（去重保序）。

    通用维度也要抽，是为了支持【跨类目对比】（如手机 vs T恤）时仍有维度可填。"""
    base = _CATEGORY_DIMENSIONS.get(category, [])
    out = list(base)
    for d in _GENERIC_DIMENSIONS:
        if d not in out:
            out.append(d)
    return out


def product_brief(detail: ProductDetail) -> str:
    """把一个商品的可对比资料压成一段喂给 LLM 的文本（离线抽取用，控制长度）。"""
    parts = [
        f"标题：{detail.title}",
        f"品牌：{detail.brand}",
        f"类目：{detail.category} / {detail.sub_category or ''}",
        f"价格区间：{price_display(detail.price_range)}",
    ]
    if detail.skus:
        spec_bits: list[str] = []
        for sku in detail.skus[:8]:
            if sku.properties:
                spec_bits.append("、".join(f"{k}:{v}" for k, v in sku.properties.items()))
        if spec_bits:
            parts.append("规格示例：" + "；".join(spec_bits[:6]))
    if detail.marketing_description:
        parts.append("卖点描述：" + detail.marketing_description[:600])
    if detail.faqs:
        faq_txt = " ".join(f"Q:{f.question} A:{f.answer}" for f in detail.faqs[:5])
        parts.append("FAQ：" + faq_txt[:700])
    if detail.reviews:
        rev_txt = " ".join(r.content for r in detail.reviews[:4])
        parts.append("用户评价：" + rev_txt[:400])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 离线索引（compare_index.json）：{product_id: {"dims": {维度: 值}, "tagline": "适合人群"}}
# ---------------------------------------------------------------------------

class CompareIndex:
    """商品对比维度的离线索引，运行时纯查表。"""

    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self._data = data

    def __len__(self) -> int:
        return len(self._data)

    def has(self, product_id: str) -> bool:
        return product_id in self._data

    def value(self, product_id: str, dimension: str) -> str:
        """查某商品某维度的预抽取值；缺失返回占位符。"""
        entry = self._data.get(product_id) or {}
        dims = entry.get("dims") or {}
        val = dims.get(dimension)
        return str(val).strip() if val else _EMPTY

    def tagline(self, product_id: str) -> str:
        """查某商品的『适合人群/定位』短语；缺失返回空串。"""
        entry = self._data.get(product_id) or {}
        return str(entry.get("tagline") or "").strip()


def load_compare_index(path: Path = COMPARE_INDEX_PATH) -> CompareIndex:
    """从磁盘加载对比索引；文件不存在时返回空索引（对比退化为只剩价格行）。"""
    if not path.exists():
        logger.warning(
            "对比索引 %s 不存在，对比将只显示价格行（请先运行 build_compare_index）。",
            path,
        )
        return CompareIndex({})
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info("对比索引已加载：%d 个商品", len(data))
    return CompareIndex(data)


@lru_cache(maxsize=1)
def get_compare_index() -> CompareIndex:
    """进程内单例，供对话 tool 与 REST 端点复用。"""
    return load_compare_index()


# ---------------------------------------------------------------------------
# 运行时构建（纯查表，零 LLM）
# ---------------------------------------------------------------------------

def _price_row(
    details: list[ProductDetail],
    selected: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """确定性价格行：有选中规格时用该规格的确定价格，否则退回价格区间；highlight=最便宜者。"""
    values: list[str] = []
    numeric: list[float] = []
    for i, d in enumerate(details):
        sku = selected[i] if selected and i < len(selected) else None
        if sku is not None:
            price = float(sku.get("price", 0) or 0)
            values.append(f"¥{price:g}")
            numeric.append(price)
        else:
            values.append(price_display(d.price_range))
            numeric.append(float(d.price_range.min_price))
    min_idx = min(range(len(details)), key=lambda i: numeric[i])
    return {"label": "价格", "values": values, "highlight": min_idx}


def _range_row(details: list[ProductDetail]) -> dict[str, Any] | None:
    """价格区间行：只有某件商品是多规格、区间和确定价不同时才有必要显示。"""
    if all(d.price_range.min_price == d.price_range.max_price for d in details):
        return None
    return {"label": "价格区间", "values": [price_display(d.price_range) for d in details], "highlight": None}


def _sku_dimension_rows(selected: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    """把选中规格的各维度拆成独立行（存储 / 颜色 / 尺码…），值取自选中规格。

    两边取值相同也照样显示：SKU 级对比首先要让用户看清"现在比的是哪两个规格"。"""
    if not any(selected):
        return []
    dims: list[str] = []
    for sku in selected:
        for dim in (sku or {}).get("options") or {}:
            if dim not in dims:
                dims.append(dim)
    return [
        {
            "label": dim,
            "values": [str(((sku or {}).get("options") or {}).get(dim) or _EMPTY) for sku in selected],
            "highlight": None,
        }
        for dim in dims
    ]


# 这些类目维度是离线 LLM 对所有规格的一句话概括（"8/12GB+256/512GB 多版本可选"）。
# 有了逐维度的 SKU 行之后它们就重复了，SKU 行存在时不再显示。
_SKU_COVERED_DIMENSIONS = {"存储规格", "容量规格", "尺码颜色", "规格分量"}


def _sku_entry(sku: dict[str, Any] | None, reason: str = "") -> dict[str, Any] | None:
    if sku is None:
        return None
    price = float(sku.get("price", 0) or 0)
    return {
        "sku_id": sku.get("sku_id"),
        "label": sku_label(sku),
        "options": dict(sku.get("options") or {}),
        "price": price,
        "price_display": f"¥{price:g}",
        "reason": reason,
    }


def _product_header(
    detail: ProductDetail,
    selected: dict[str, Any] | None = None,
    reason: str = "",
    all_skus: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "product_id": detail.product_id,
        "title": detail.title,
        "brand": detail.brand,
        "price": float(selected.get("price", 0) or 0) if selected else detail.price_range.min_price,
        "image_url": _IMAGE_MANIFEST.get(detail.product_id) or detail.image_url,
        "price_range": {
            "min": detail.price_range.min_price,
            "max": detail.price_range.max_price,
            "sku_count": detail.price_range.sku_count,
        },
        "sku": _sku_entry(selected, reason),
    }
    if all_skus:
        header["skus"] = [_sku_entry(s) for s in all_skus]
    return header


def select_skus_for(
    details: list[ProductDetail],
    *,
    selections: dict[str, str] | None = None,
    query: str = "",
    budget_max: float | None = None,
    budget_min: float | None = None,
) -> list[tuple[dict[str, Any] | None, str, list[dict[str, Any]]]]:
    """给每件商品选一个确定规格：返回 [(selected_sku, reason, all_skus)]。"""
    out = []
    for d in details:
        skus = [sku_to_dict(s) for s in (d.skus or []) if str(getattr(s, "status", "active")) == "active"]
        sku, reason = select_sku(
            skus,
            query=query,
            sku_id=(selections or {}).get(d.product_id),
            budget_max=budget_max,
            budget_min=budget_min,
        )
        out.append((sku, reason, skus))
    return out


def build_comparison(
    details: list[ProductDetail],
    focus: str = "",
    timeout: float | None = None,
    *,
    selections: dict[str, str] | None = None,
    query: str = "",
    budget_max: float | None = None,
    budget_min: float | None = None,
) -> dict[str, Any]:
    """构建完整对比结构：SKU 级价格 / 规格行确定性生成 + 其余维度从离线索引查表。

    至少 2 个商品。运行时不调用任何 LLM。``focus``/``timeout`` 仅为向后兼容保留。
    selections: {product_id: sku_id} 显式指定规格；query / budget 用于自动选规格。
    """
    if len(details) < 2:
        raise ValueError("对比至少需要 2 个商品")

    index = get_compare_index()
    dimensions = _dimensions_for(details)
    picked = select_skus_for(
        details, selections=selections, query=query, budget_max=budget_max, budget_min=budget_min,
    )
    selected = [p[0] for p in picked]

    rows: list[dict[str, Any]] = [_price_row(details, selected)]
    range_row = _range_row(details)
    if range_row is not None:
        rows.append(range_row)
    sku_rows = _sku_dimension_rows(selected)
    rows.extend(sku_rows)
    for dim in dimensions:
        if sku_rows and dim in _SKU_COVERED_DIMENSIONS:
            continue
        values = [index.value(d.product_id, dim) for d in details]
        # 文本型维度不强行判优，把判断交给用户；价格行才高亮（确定性）。
        rows.append({"label": dim, "values": values, "highlight": None})

    titles = " vs ".join(d.title[:16] for d in details)
    return {
        "title": f"对比：{titles}",
        "products": [
            _product_header(d, sku, reason, all_skus)
            for d, (sku, reason, all_skus) in zip(details, picked)
        ],
        "rows": rows,
        # 选购建议暂不提供：只给客观维度表，让用户自己判断。保留空字段做向后兼容。
        "recommendation": "",
    }

