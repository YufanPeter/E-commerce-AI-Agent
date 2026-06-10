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


def test_multiple_name_matches_asks_clarification(monkeypatch):
    # 这组测试验证「澄清状态机」，与 LLM 消歧无关 → stub 成「定不了」保证确定性。
    import agent.tools.resolve as rv
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)
    session = _session_with_hits()
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "小米那款详细说说", session, {}
    )

    assert result.needs_composer is False
    assert "哪一款" in (result.narrative_override or "")
    # 追问时必须记住候选子集 + 用户原本想问的话，供下一轮把“第一款”正确归位。
    pending = session.get("pending_detail")
    assert pending is not None
    assert [c["product_id"] for c in pending["candidates"]] == ["p2", "p3"]


def test_pending_ordinal_reply_maps_to_candidate_subset_not_global_list(monkeypatch):
    """复现并验证截图 bug：澄清后回答“第一款”应命中候选子集第 1 个，而非 last_hits[0]。"""
    import agent.tools.resolve as rv
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)
    session = _session_with_hits()
    tool = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever())

    first = tool.run("小米的续航怎么样", session, {})
    assert first.needs_composer is False  # 先澄清
    assert session.get("pending_detail") is not None

    second = tool.run("第一款", session, {})
    # 候选是 [p2, p3]，“第一款”=p2（小米平板），绝不是 last_hits[0]=p1（理肤泉）。
    assert second.payload["product"]["product_id"] == "p2"
    # 原始问的“续航”focus 必须被保留，而不是退化成 general。
    assert second.payload["focus_aspect"] == "performance"
    # 解析成功后待定态清空，避免粘住下一轮。
    assert session.get("pending_detail") is None


def test_pending_brand_reply_maps_to_candidate(monkeypatch):
    import agent.tools.resolve as rv
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)
    session = _session_with_hits()
    tool = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever())

    tool.run("小米的续航怎么样", session, {})
    second = tool.run("第二款", session, {})
    assert second.payload["product"]["product_id"] == "p3"


def test_pending_topic_change_releases_clarification(monkeypatch):
    import agent.tools.resolve as rv
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)
    session = _session_with_hits()
    tool = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever())

    tool.run("小米的续航怎么样", session, {})
    assert session.get("pending_detail") is not None

    # 用户换了话题（点名另一商品的属性），不应再纠结在候选里。
    third = tool.run("理肤泉那款敏感肌能用吗", session, {})
    assert third.payload["product"]["product_id"] == "p1"
    assert session.get("pending_detail") is None



def test_focus_product_disambiguates_multiple_name_matches():
    session = _session_with_hits()
    session.set("last_focus_product_id", "p2")

    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "小米的续航怎么样", session, {}
    )

    assert result.needs_composer is True
    assert result.payload["product"]["product_id"] == "p2"
    assert result.payload["focus_aspect"] == "performance"


def test_bare_attribute_followup_uses_focused_product():
    session = _session_with_hits()
    session.set("last_focus_product_id", "p2")

    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "续航怎么样", session, {}
    )

    assert result.payload["product"]["product_id"] == "p2"


def test_general_detail_uses_single_paragraph_intro_hint():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "第一个怎么样", _session_with_hits(), {}
    )

    assert result.payload["focus_aspect"] == "general"
    assert result.composer_hint is not None
    assert "1 个自然段" in result.composer_hint
    assert "不要分点" in result.composer_hint
    assert "不要 emoji" in result.composer_hint
    assert "90 个中文字符" in result.composer_hint
    assert "禁止写成长段参数介绍" in result.composer_hint
    assert "不要写成用户评价总结" in result.composer_hint


def test_review_intent_keywords_use_review_focus():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "第一个用户反馈怎么样", _session_with_hits(), {}
    )

    assert result.payload["focus_aspect"] == "reviews"
    assert result.composer_hint is not None
    assert "🌟 优点" in result.composer_hint
    assert "🔍 缺点" in result.composer_hint
    assert "不要写成商品参数介绍" in result.composer_hint


def test_review_focus_uses_review_evidence_fallback():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "第一个评价怎么样", _session_with_hits(), {}
    )

    evidence = result.payload["evidence"]
    assert result.payload["focus_aspect"] == "reviews"
    assert result.composer_hint is not None
    assert "✨ 总结" in result.composer_hint
    assert "🌟 优点" in result.composer_hint
    assert "🔍 缺点" in result.composer_hint
    assert "每段不超过 70 个中文字符" in result.composer_hint
    assert "2-3 个核心点" in result.composer_hint
    assert evidence[0]["source_type"] == "user_review"
    assert any("清爽" in item["text"] for item in evidence)


def test_negative_review_focus_prefers_negative_reviews():
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "第一个有没有差评", _session_with_hits(), {}
    )

    evidence = result.payload["evidence"]
    assert result.payload["focus_aspect"] == "negative_reviews"
    assert result.composer_hint is not None
    assert "缺点段" in result.composer_hint
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


# --------------------- LLM 语义消歧（截图 bug：「华为耳机」死循环） ---------------------

def test_llm_disambiguates_multiple_name_matches(monkeypatch):
    """品牌相同、品类不同（「小米平板」vs「小米手机」），规则两个都中→交给 LLM 挑唯一一款。"""
    import agent.tools.product_detail as pd

    captured = {}

    def fake_pick(query, candidates, timeout=5.0):
        captured["candidates"] = candidates
        # 模拟 LLM 看懂「平板」对应子品类→选中 p2
        for i, c in enumerate(candidates):
            if "平板" in str(c.get("sub_category", "")):
                return i
        return None

    import agent.tools.resolve as rv
    monkeypatch.setattr(rv, "llm_pick_candidate", fake_pick)

    session = _session_with_hits()
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "小米平板那款详细说说", session, {}
    )

    # 直接命中 p2，不再反问、不留 pending。
    assert result.needs_composer is True
    assert result.payload["product"]["product_id"] == "p2"
    assert session.get("pending_detail") is None
    # 候选必须带上结构化字段，LLM 才有依据。
    assert "sub_category" in captured["candidates"][0]


def test_llm_unavailable_falls_back_to_clarification(monkeypatch):
    """LLM 不可用（返回 None）时，必须安全降级为反问，绝不乱选。"""
    import agent.tools.resolve as rv

    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)

    session = _session_with_hits()
    result = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever()).run(
        "小米那款详细说说", session, {}
    )

    assert result.needs_composer is False
    assert "哪一款" in (result.narrative_override or "")
    assert session.get("pending_detail") is not None


def test_llm_picks_within_pending_candidates(monkeypatch):
    """澄清待定后，用户回「那个平板」——序号/品牌子串都不唯一，LLM 在候选子集里挑中。"""
    import agent.tools.resolve as rv

    # 第一轮先制造 pending（关掉 LLM，强制走反问）。
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)
    session = _session_with_hits()
    tool = ProductDetailTool(product_store=_FakeStore(), evidence_retriever=_EmptyRetriever())
    first = tool.run("小米的续航怎么样", session, {})
    assert first.needs_composer is False
    assert session.get("pending_detail") is not None

    # 第二轮打开 LLM：在候选 [p2, p3] 里按子品类挑中平板 p2。
    def fake_pick(query, candidates, timeout=5.0):
        for i, c in enumerate(candidates):
            if "平板" in str(c.get("sub_category", "")):
                return i
        return None

    monkeypatch.setattr(rv, "llm_pick_candidate", fake_pick)
    second = tool.run("要那个平板", session, {})
    assert second.payload["product"]["product_id"] == "p2"
    # 原始「续航」focus 仍要保留。
    assert second.payload["focus_aspect"] == "performance"
    assert session.get("pending_detail") is None


def test_llm_pick_candidate_returns_none_on_few_candidates():
    """护栏：候选少于 2 个或 query 为空时，不该浪费一次 LLM 调用，直接 None。"""
    from agent.tools.llm_match import llm_pick_candidate

    assert llm_pick_candidate("华为耳机", []) is None
    assert llm_pick_candidate("华为耳机", [{"product_id": "p1", "title": "只有一个"}]) is None
    assert llm_pick_candidate("", [{"product_id": "p1"}, {"product_id": "p2"}]) is None
