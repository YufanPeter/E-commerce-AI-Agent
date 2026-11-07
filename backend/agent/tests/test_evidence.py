from __future__ import annotations

from types import SimpleNamespace

from agent.composer import _fallback_structured, _trim_payload_for_llm
from agent.tools.base import ToolResult
from agent.tools.evidence import MAX_CHARS, card_evidence
from rag.retriever import RetrievedChunk


PREFIX = "商品：某手机 品牌：某 类目：数码 / 手机 基础价格：100元 "


def _chunk(kind, body, polarity=None):
    meta = {"chunk_type": kind, "product_id": "p"}
    if polarity:
        meta["polarity"] = polarity
    marker = "回答：" if kind == "official_faq" else "内容："
    return RetrievedChunk(f"{kind}-{body[:4]}", f"{PREFIX}知识类型：x {marker}{body}", meta, 0.2)


def _hit(*chunks):
    return SimpleNamespace(evidence=list(chunks))


def test_prefix_is_removed_and_sources_are_labelled():
    evidence = card_evidence(_hit(
        _chunk("marketing_description", "搭载 6800mAh 大电池，重度使用一天半。"),
        _chunk("official_faq", "支持 120W 快充，半小时充满。"),
    ), "续航长的手机")
    assert [p["source"] for p in evidence["points"]] == ["商品描述", "官方问答"]
    assert "商品：" not in evidence["points"][0]["text"]
    assert "6800mAh" in evidence["points"][0]["text"]


def test_negative_review_becomes_risk_not_selling_point():
    evidence = card_evidence(_hit(
        _chunk("user_review", "续航比宣传的短，一天要充两次。", "negative"),
        _chunk("marketing_description", "大电池长续航。"),
    ), "续航")
    assert evidence["risk"].startswith("续航比宣传的短")
    assert all("充两次" not in p["text"] for p in evidence["points"])


def test_service_complaint_is_not_a_product_risk():
    evidence = card_evidence(_hit(
        _chunk("user_review", "物流太慢，客服也不回。", "negative"),
        _chunk("marketing_description", "大电池。"),
    ), "手机")
    assert "risk" not in evidence


def test_profile_chunk_has_no_usable_body():
    assert card_evidence(_hit(RetrievedChunk("c", f"{PREFIX}知识类型：商品基础信息 SKU规格：256GB", {"chunk_type": "product_profile"}, 0.2)), "手机") is None


def test_excerpt_prefers_the_sentence_about_the_query():
    long_text = "外观采用玻璃机身，手感很好。" * 6 + "夜景模式下暗光拍照依然清晰。"
    evidence = card_evidence(_hit(_chunk("marketing_description", long_text)), "夜景拍照")
    assert "夜景" in evidence["points"][0]["text"]
    assert len(evidence["points"][0]["text"]) <= MAX_CHARS


def test_composer_receives_curated_evidence_but_not_raw_chunks():
    curated = {"points": [{"source": "商品描述", "text": "大电池"}]}
    out = _trim_payload_for_llm({"products": [
        {"product_id": "a", "title": "A", "evidence": curated},
        {"product_id": "b", "title": "B", "evidence": ["原始 chunk" * 1000]},
    ]})
    assert out["hits"][0]["evidence"] == curated
    assert "evidence" not in out["hits"][1]


def test_fallback_description_uses_evidence_when_model_is_down():
    result = ToolResult(tool_name="recommend", payload={"products": [
        {"product_id": "a", "title": "A", "evidence": {"points": [{"source": "商品描述", "text": "6800mAh 大电池"}]}},
        {"product_id": "b", "title": "B"},
    ]})
    items = _fallback_structured(result)["items"]
    assert items[0]["description"] == "6800mAh 大电池"
    assert "B" in items[1]["description"]
