from __future__ import annotations

from types import SimpleNamespace

from agent.session import AgentSession
from agent.tools.product_detail import ProductDetailTool
from store.product_store import PriceRange, ProductDetail, ProductFaq, ProductReview


_HITS = [
    {"product_id": "p1", "title": "理肤泉 特护清盈防晒乳"},
    {"product_id": "p2", "title": "小米平板 8 Pro 高刷大屏"},
    {"product_id": "p3", "title": "小米 17 Ultra 影像手机"},
]


def _detail(pid: str, title: str, brand: str = "品牌") -> ProductDetail:
    return ProductDetail(
        product_id=pid,
        title=title,
        brand=brand,
        category="数码电子" if "小米" in title else "美妆护肤",
        sub_category="平板电脑" if "平板" in title else "防晒",
        base_price=100,
        image_path=None,
        image_url=None,
        price_range=PriceRange(min_price=100, max_price=120, sku_count=2),
        skus=[],
        marketing_description=f"{title} 的商品描述，适合日常使用。",
        faqs=[
            ProductFaq(source_index=0, question="适合敏感肌吗？", answer="建议先局部试用。"),
        ],
        reviews=[
            ProductReview(source_index=0, nickname="阿凯", rating=5, content="用起来清爽，整体很满意。", polarity="positive"),
            ProductReview(source_index=1, nickname="小雨", rating=2, content="感觉有点拔干，干皮要谨慎。", polarity="negative"),
        ],
    )


class _FakeStore:
    def __init__(self):
        self.details = {
            "p1": _detail("p1", "理肤泉 特护清盈防晒乳", "理肤泉"),
            "p2": _detail("p2", "小米平板 8 Pro 高刷大屏", "小米"),
            "p3": _detail("p3", "小米 17 Ultra 影像手机", "小米"),
        }

    def get_product_detail(self, product_id: str):
        return self.details.get(product_id)


class _EmptyRetriever:
    def search(self, *args, **kwargs):
        return []


class _FakeRetriever:
    def __init__(self):
        self.calls = []

    def search(self, query: str, top_k: int, where: dict):
        self.calls.append({"query": query, "top_k": top_k, "where": where})
        return [
            SimpleNamespace(
                document="用户说屏幕观感细腻，适合追剧和学习。",
                metadata={"product_id": "p2", "chunk_type": "user_review", "title": "用户评价", "polarity": "positive"},
                distance=0.2,
                chunk_type="user_review",
            )
        ]


class _MixedRetriever:
    def search(self, query: str, top_k: int, where: dict):
        return [
            SimpleNamespace(
                document="官方描述：屏幕大、性能强。",
                metadata={"product_id": "p2", "chunk_type": "marketing_description", "title": "商品描述"},
                distance=0.1,
                chunk_type="marketing_description",
            ),
            SimpleNamespace(
                document="用户说屏幕好但配件偏贵。",
                metadata={"product_id": "p2", "chunk_type": "user_review", "title": "用户评价", "polarity": "neutral"},
                distance=0.4,
                chunk_type="user_review",
            ),
        ]


def _session_with_hits(hits=None) -> AgentSession:
    session = AgentSession(session_id="t")
    session.set("last_hits", hits or _HITS)
    return session


def test_no_last_hits_asks_for_context():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "这款评价怎么样", AgentSession(), {}
    )

    assert result.needs_composer is False
    assert "先让我推荐" in (result.narrative_override or "")


def test_resolves_explicit_index_to_second_product():
    session = _session_with_hits()
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "第二个详细说说", session, {}
    )

    assert result.needs_composer is True
    assert result.payload["product"]["product_id"] == "p2"
    assert session.get("last_focus_product_id") == "p2"


def test_resolves_title_keyword():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "理肤泉那款敏感肌能用吗", _session_with_hits(), {}
    )

    assert result.payload["product"]["product_id"] == "p1"
    assert result.payload["focus_aspect"] == "sensitive_skin"


def test_multiple_name_matches_asks_clarification():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "小米那款详细说说", _session_with_hits(), {}
    )

    assert result.needs_composer is False
    assert "哪一款" in (result.narrative_override or "")


def test_review_focus_uses_review_evidence_fallback():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "第一个评价怎么样", _session_with_hits(), {}
    )

    evidence = result.payload["evidence"]
    assert result.payload["focus_aspect"] == "reviews"
    assert evidence[0]["source_type"] == "user_review"
    assert any("清爽" in item["text"] for item in evidence)


def test_negative_review_focus_prefers_negative_reviews():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "第一个有没有差评", _session_with_hits(), {}
    )

    evidence = result.payload["evidence"]
    assert result.payload["focus_aspect"] == "negative_reviews"
    assert evidence[0]["source_type"] == "user_review"
    assert evidence[0]["metadata"]["polarity"] == "negative"


def test_rag_evidence_is_filtered_to_target_product():
    retriever = _FakeRetriever()
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=retriever).run(
        "第二个适合学习吗", _session_with_hits(), {}
    )

    assert retriever.calls[-1]["where"] == {"product_id": "p2"}
    assert result.payload["evidence"][0]["text"] == "用户说屏幕观感细腻，适合追剧和学习。"
    assert result.payload["evidence"][0]["metadata"]["product_id"] == "p2"


def test_review_question_prioritizes_review_chunks_from_rag():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_MixedRetriever()).run(
        "第二个评价怎么样", _session_with_hits(), {}
    )

    assert result.payload["evidence"][0]["source_type"] == "user_review"
    assert "配件偏贵" in result.payload["evidence"][0]["text"]
