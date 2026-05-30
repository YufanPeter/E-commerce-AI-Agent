"""指代消解 + compare/product_detail 工具的回归测试。

数据层用轻量 fake，不碰真实 SQLite，可在 CI 无脑跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.session import AgentSession
from agent.tools.compare import CompareTool
from agent.tools.product_detail import ProductDetailTool
from agent.tools.reference import resolve_by_title, resolve_indices


# --------------------------- resolve_indices ---------------------------


def test_resolve_ordinal_single():
    assert resolve_indices("第二个详细说说", 3) == [1]


def test_resolve_ordinal_multi():
    assert resolve_indices("第一个和第三个", 5) == [0, 2]


def test_resolve_bare_numbers():
    assert resolve_indices("1和3", 5) == [0, 2]


def test_resolve_front_n():
    assert resolve_indices("前两个", 5) == [0, 1]


def test_resolve_pair_phrase():
    assert resolve_indices("这俩", 5) == [0, 1]


def test_resolve_out_of_range_dropped():
    assert resolve_indices("第九个", 3) == []


def test_resolve_no_match_returns_empty():
    assert resolve_indices("对比一下", 5) == []


def test_resolve_by_title_hits():
    hits = [{"title": "欧莱雅面霜"}, {"title": "珀莱雅双抗精华"}]
    assert resolve_by_title("那个珀莱雅的", hits) == 1


def test_resolve_by_title_no_match():
    hits = [{"title": "欧莱雅面霜"}]
    assert resolve_by_title("随便", hits) is None


# --------------------------- fake 数据层 ---------------------------


@dataclass(frozen=True)
class _FakePriceRange:
    min_price: float
    max_price: float
    sku_count: int = 1


@dataclass(frozen=True)
class _FakeReview:
    rating: int
    polarity: str
    content: str


@dataclass(frozen=True)
class _FakeFaq:
    question: str
    answer: str


@dataclass(frozen=True)
class _FakeDetail:
    product_id: str
    title: str
    brand: str
    category: str
    sub_category: str | None
    price_range: _FakePriceRange
    marketing_description: str = "卖点文案"
    faqs: list[_FakeFaq] = field(default_factory=list)
    reviews: list[_FakeReview] = field(default_factory=list)


class _FakeStore:
    def __init__(self, details: dict[str, _FakeDetail]):
        self._details = details

    def get_product_detail(self, pid: str):
        return self._details.get(pid)


def _make_store() -> _FakeStore:
    return _FakeStore({
        "p1": _FakeDetail(
            "p1", "珀莱雅双抗精华", "珀莱雅", "美妆护肤", "精华",
            _FakePriceRange(129, 129),
            faqs=[_FakeFaq("能敏感肌用吗", "可以")],
            reviews=[_FakeReview(5, "positive", "好用"), _FakeReview(4, "positive", "不错")],
        ),
        "p2": _FakeDetail(
            "p2", "兰蔻小黑瓶", "兰蔻", "美妆护肤", "精华",
            _FakePriceRange(680, 680),
            reviews=[_FakeReview(3, "neutral", "一般")],
        ),
    })


def _session_with_hits() -> AgentSession:
    sess = AgentSession()
    sess.remember_search(
        {"category": "美妆护肤"},
        [{"product_id": "p1", "title": "珀莱雅双抗精华"},
         {"product_id": "p2", "title": "兰蔻小黑瓶"}],
    )
    return sess


# --------------------------- ProductDetailTool ---------------------------


def test_detail_resolves_second_and_loads_full():
    tool = ProductDetailTool(product_store=_make_store())
    r = tool.run("第二个详细说说", _session_with_hits(), {})
    assert r.tool_name == "product_detail"
    assert r.needs_composer is True
    assert r.payload["selected_index"] == 1
    assert r.payload["product"]["title"] == "兰蔻小黑瓶"
    assert r.payload["focus_aspect"] == "第二个详细说说"


def test_detail_resolves_by_title():
    tool = ProductDetailTool(product_store=_make_store())
    r = tool.run("那个珀莱雅的成分能说说吗", _session_with_hits(), {})
    assert r.payload["product"]["title"] == "珀莱雅双抗精华"


def test_detail_no_memory_asks_to_recommend():
    tool = ProductDetailTool(product_store=_make_store())
    r = tool.run("第一个详细点", AgentSession(), {})
    assert r.needs_composer is False
    assert "推荐" in r.narrative_override


def test_detail_writes_focus_product_to_memory():
    sess = _session_with_hits()
    ProductDetailTool(product_store=_make_store()).run("第一个", sess, {})
    assert sess.get("last_focus_product_id") == "p1"


# --------------------------- CompareTool ---------------------------


def test_compare_default_first_two():
    tool = CompareTool(product_store=_make_store())
    r = tool.run("对比一下", _session_with_hits(), {})
    assert r.tool_name == "compare"
    assert r.needs_composer is True
    assert [p["product_id"] for p in r.payload["products"]] == ["p1", "p2"]
    labels = [d["label"] for d in r.payload["dimensions"]]
    assert "价格" in labels and "好评概览" in labels


def test_compare_review_summary_reflects_data():
    tool = CompareTool(product_store=_make_store())
    r = tool.run("对比第一个和第二个", _session_with_hits(), {})
    review_dim = next(d for d in r.payload["dimensions"] if d["label"] == "好评概览")
    # p1 有 2 条好评，p2 仅 1 条中性
    assert "好评2条" in review_dim["values"][0]
    assert "暂无评价" not in review_dim["values"][0]


def test_compare_needs_two_hits():
    sess = AgentSession()
    sess.remember_search({}, [{"product_id": "p1", "title": "X"}])
    r = CompareTool(product_store=_make_store()).run("对比", sess, {})
    assert r.needs_composer is False
    assert "推荐" in r.narrative_override
