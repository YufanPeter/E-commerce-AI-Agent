from __future__ import annotations

"""解析结果的事实校验：这些都是召回评测里真实出现过的错误。"""

from search.llm_parser import _clean_negatives, _clean_unavailable, _to_parsed_query, _verify_sub_category, term_is_negated_in_query
from search.query_understanding import ParsedQuery, brand_is_named_in
from search.search_service import (
    ProductHit,
    SearchService,
    _apply_profile_boost,
    _apply_user_profile,
    _is_weak_category_guess,
    _mentions_positively,
)
from rag.retriever import RetrievedChunk


TAXONOMY = {
    "sub_to_cat": {"精华": "美妆护肤", "化妆水": "美妆护肤", "牛奶": "食品饮料", "咖啡": "食品饮料", "平板电脑": "数码电子"},
    "brands": ["雀巢", "SK-II", "兰蔻", "资生堂"],
    "brand_expand": {"雀巢": ["雀巢"], "SK-II": ["SK-II"], "兰蔻": ["兰蔻"], "资生堂": ["资生堂"]},
    "titles": [("SK-II护肤精华露神仙水晶透", "化妆水"), ("雀巢咖啡 1+2原味", "咖啡"), ("某品牌100条装牛奶", "牛奶")],
    "catalog_text": "sk-ii护肤精华露神仙水晶透 小棕瓶 飞马系列 wi-fi",
    "brand_subs": {"雀巢": ["咖啡"]},
}


def test_wanted_attribute_is_not_an_exclusion():
    assert not term_is_negated_in_query("敏感肌", "敏感肌能用的防晒")
    assert not term_is_negated_in_query("防水", "下雨天爬山穿的防水鞋")
    assert _clean_negatives(["敏感肌"], "敏感肌能用的防晒") == []


def test_real_exclusions_are_kept():
    assert _clean_negatives(["糖"], "无糖的茶饮") == ["糖"]
    assert _clean_negatives(["糖"], "0糖饮料") == ["糖"]
    assert _clean_negatives(["辣"], "不辣的零食") == ["辣"]
    assert _clean_negatives(["花生"], "不要花生的") == ["花生"]


def test_product_saying_it_lacks_a_term_is_not_banned():
    assert not _mentions_positively("无香料、酒精、防腐剂", "酒精")
    assert not _mentions_positively("没有额外添加糖", "糖")
    assert not _mentions_positively("适合控糖人群的饮品", "糖")
    assert _mentions_positively("配料：白砂糖、水", "糖")
    assert _mentions_positively("东鹏特饮 250ml 功能饮料", "功能饮料")


def test_faq_comparison_does_not_ban_but_confirmed_avoid_term_is_strict():
    from search.search_service import _chunk_is_banned

    faq = RetrievedChunk("c", "可以自己加桂花糖浆调味", {"chunk_type": "official_faq", "title": "黑咖啡", "product_id": "p"}, 0.2)
    assert not _chunk_is_banned(faq, ["糖"], set(), set(), set())
    assert _chunk_is_banned(faq, ["糖"], set(), set(), set(), strict_terms={"糖"})


def test_guessed_brand_is_not_a_hard_filter():
    assert not brand_is_named_in("兰蔻", "红腰子精华")
    assert brand_is_named_in("Apple 苹果", "苹果的平板")
    assert brand_is_named_in("阿迪达斯", "adidas 卫衣")
    parsed = _to_parsed_query("红腰子精华", {"brand_include": ["兰蔻"], "sub_category": "精华"}, TAXONOMY)
    assert parsed.brand_include == []


def test_sub_category_must_agree_with_named_product():
    assert _verify_sub_category("精华", "神仙水怎么样", [], TAXONOMY) is None
    assert _verify_sub_category("化妆水", "神仙水怎么样", [], TAXONOMY) == "化妆水"
    assert _verify_sub_category("牛奶", "雀巢1+2孕妇能喝吗", ["雀巢"], TAXONOMY) is None


def test_price_digits_do_not_anchor_a_product():
    assert _verify_sub_category("平板电脑", "100以内的平板", [], TAXONOMY) == "平板电脑"


def test_unavailable_terms_are_checked_against_catalog():
    assert _clean_unavailable(["小棕瓶"], "小棕瓶", TAXONOMY) == []
    assert _clean_unavailable(["飞马41"], "飞马41", TAXONOMY) == []
    assert _clean_unavailable(["扫地机器人"], "扫地机器人", TAXONOMY) == ["扫地机器人"]
    assert _clean_unavailable([], "Dior 口红", TAXONOMY) == ["Dior"]
    # 点名了库里的品牌，英文词就不是在点另一个商品
    assert _clean_unavailable([], "雀巢 wifi", TAXONOMY) == []


def _hit(pid, score, brand="B", low=100.0, high=100.0):
    return ProductHit(
        product_id=pid, title=pid, brand=brand, category="c", sub_category="s", base_price=low,
        score=score, best_distance=0.2, rerank_score=None, min_price=low, max_price=high,
    )


def test_profile_budget_is_soft_not_a_filter():
    parsed = ParsedQuery(original_query="推荐笔记本", retrieval_query="笔记本")
    merged = _apply_user_profile(parsed, {"budget_max": 500, "favorite_categories": ["美妆护肤"]})
    assert merged.max_price is None
    assert merged.category is None


def test_soft_budget_reorders_but_keeps_everything():
    hits = [_hit("expensive", 0.90, low=5000, high=5000), _hit("cheap", 0.85, low=300, high=300), _hit("floor", 0.1, low=6000, high=6000)]
    parsed = ParsedQuery(original_query="q")
    ranked, signals = _apply_profile_boost(hits, parsed, {"budget_max": 500})
    assert [h.product_id for h in ranked] == ["cheap", "expensive", "floor"]
    assert signals[0]["kind"] == "budget"


def test_budget_cannot_override_clear_relevance():
    hits = [_hit("relevant", 0.95, low=5000, high=5000), _hit("cheap_but_off", 0.2, low=300, high=300), _hit("floor", 0.1, low=6000, high=6000)]
    ranked, _ = _apply_profile_boost(hits, ParsedQuery(original_query="q"), {"budget_max": 500})
    assert ranked[0].product_id == "relevant"


def test_budget_decides_when_relevance_is_nearly_tied():
    hits = [_hit("iphone", 0.63, low=8999, high=12499), _hit("xiaomi", 0.60, low=6499, high=7099), _hit("reno", 0.56, low=3299, high=3699)]
    ranked, _ = _apply_profile_boost(hits, ParsedQuery(original_query="推荐手机"), {"budget_max": 3500})
    assert ranked[0].product_id == "reno"


def test_turn_price_wins_over_profile_budget():
    hits = [_hit("a", 0.9, low=5000, high=5000), _hit("b", 0.85, low=300, high=300)]
    parsed = ParsedQuery(original_query="q", max_price=9000)
    ranked, signals = _apply_profile_boost(hits, parsed, {"budget_max": 500})
    assert [h.product_id for h in ranked] == ["a", "b"]
    assert signals == []


def test_category_scoped_budget_beats_global():
    hits = [_hit("mid", 0.9, low=3000, high=3000), _hit("low", 0.85, low=400, high=400)]
    parsed = ParsedQuery(original_query="q", sub_category="智能手机")
    profile = {"budget_max": 500, "category_specific": {"智能手机": {"budget_max": 3500}}}
    ranked, signals = _apply_profile_boost(hits, parsed, profile)
    assert ranked[0].product_id == "mid"
    assert signals[0]["scope"] == "智能手机"


def test_liked_brand_lifts_but_does_not_outrank_clear_relevance():
    close = [_hit("other", 0.90, brand="X"), _hit("liked", 0.88, brand="小米"), _hit("floor", 0.1, brand="Y")]
    ranked, _ = _apply_profile_boost(close, ParsedQuery(original_query="q"), {"brand_include": ["小米"]})
    assert ranked[0].product_id == "liked"
    far = [_hit("best", 0.95, brand="X"), _hit("liked", 0.1, brand="小米"), _hit("mid", 0.5, brand="Y")]
    ranked, _ = _apply_profile_boost(far, ParsedQuery(original_query="q"), {"brand_include": ["小米"]})
    assert ranked[0].product_id == "best"


def test_personalization_off_changes_nothing():
    hits = [_hit("a", 0.9), _hit("b", 0.8, brand="小米")]
    ranked, signals = _apply_profile_boost(hits, ParsedQuery(original_query="q"), {"personalization_enabled": False, "brand_include": ["小米"]})
    assert [h.product_id for h in ranked] == ["a", "b"]
    assert signals == []


def test_avoid_terms_become_hard_exclusions():
    merged = _apply_user_profile(ParsedQuery(original_query="q"), {"avoid_terms": ["酒精"]})
    assert merged.negative_ingredients == ["酒精"]


def _chunk(distance, index=None):
    meta = {"product_id": "p"}
    if index:
        meta["_index"] = index
    return RetrievedChunk("c", "d", meta, distance)


def test_weak_category_guess_is_treated_as_out_of_catalog():
    guess = ParsedQuery(original_query="机械键盘", category="数码电子")
    assert _is_weak_category_guess(guess, [_chunk(0.48)])
    assert not _is_weak_category_guess(guess, [_chunk(0.3)])
    assert not _is_weak_category_guess(guess, [_chunk(0.48, index="fallback")])
    assert not _is_weak_category_guess(ParsedQuery(original_query="推荐点吃的", category="食品饮料"), [_chunk(0.52)])
    assert not _is_weak_category_guess(ParsedQuery(original_query="耳机", sub_category="真无线耳机"), [_chunk(0.6)])


def test_unavailable_terms_short_circuit_search(monkeypatch):
    parsed = ParsedQuery(original_query="扫地机器人", unavailable_terms=["扫地机器人"])
    monkeypatch.setattr("search.search_service.understand_query", lambda query: parsed)

    class _Retriever:
        def search(self, **kwargs):
            raise AssertionError("库外词不应该再做检索")

    result = SearchService(retriever=_Retriever(), use_rerank=False).search("扫地机器人")
    assert result.hits == []
    assert result.parsed.unavailable_terms == ["扫地机器人"]


def test_one_product_cannot_fill_the_candidate_pool():
    from search.search_service import _limit_chunks_per_product

    chunks = [RetrievedChunk(f"a{i}", "d", {"product_id": "a"}, 0.1) for i in range(10)]
    chunks.append(RetrievedChunk("b0", "d", {"product_id": "b"}, 0.5))
    kept = _limit_chunks_per_product(chunks)
    assert [c.product_id for c in kept].count("a") == 4
    assert kept[-1].product_id == "b"
