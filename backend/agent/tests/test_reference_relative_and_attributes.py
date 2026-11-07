"""「上一个 / 下一个」相对指代 与 「黑色那个 / 42 码的」规格值指代。"""

from __future__ import annotations

from types import SimpleNamespace

import agent.tools.resolve as rv
from agent.session import AgentSession
from agent.tools.reference import resolve_by_name, resolve_indices, resolve_relative_index
from agent.tools.resolve import enrich_candidates, focus_index_in, resolve_one


_HITS = [
    {"product_id": "p1", "title": "耐克 Pegasus 41 跑步鞋", "brand": "耐克",
     "options": {"颜色": ["黑色", "白色"], "尺码": ["41", "42"]}},
    {"product_id": "p2", "title": "阿迪达斯 Ultraboost 跑步鞋", "brand": "阿迪达斯",
     "options": {"颜色": ["白色", "蓝色"], "尺码": ["41", "42"]}},
    {"product_id": "p3", "title": "亚瑟士 Kayano 跑步鞋", "brand": "亚瑟士",
     "options": {"颜色": ["红色"], "尺码": ["42"]}},
]


def _no_llm(monkeypatch):
    monkeypatch.setattr(rv, "llm_pick_candidate", lambda *a, **k: None)
    monkeypatch.setattr(rv, "llm_pick_candidates", lambda *a, **k: [])


class TestRelative:
    def test_prev_and_next_anchor_on_focus(self):
        assert resolve_relative_index("上一个呢", 3, focus_index=1) == 0
        assert resolve_relative_index("前一款详细说说", 3, focus_index=2) == 1
        assert resolve_relative_index("下一个", 3, focus_index=1) == 2
        assert resolve_relative_index("后面那款", 3, focus_index=0) == 1

    def test_out_of_range_or_no_focus_is_none(self):
        assert resolve_relative_index("上一个", 3, focus_index=0) is None
        assert resolve_relative_index("下一个", 3, focus_index=2) is None
        assert resolve_relative_index("上一个", 3, focus_index=None) is None
        assert resolve_relative_index("第二个", 3, focus_index=1) is None
        # 「最后一个」是绝对序号，不能被「后一个」劫持成「下一个」
        assert resolve_relative_index("第一个和最后一个", 3, focus_index=0) is None
        assert resolve_indices("第一个和最后一个", 3, focus_index=0) == [0, 2]

    def test_resolve_indices_uses_focus_and_never_misreads_前一个_as_first(self):
        assert resolve_indices("前一个", 3, focus_index=2) == [1]
        # 没有焦点：不能把「前一个」当成「前 1 个 = 第一个」，交上层反问
        assert resolve_indices("前一个", 3) == []
        # 普通序号不受影响
        assert resolve_indices("前两个", 3, focus_index=2) == [0, 1]
        assert resolve_indices("第三个", 3, focus_index=0) == [2]

    def test_focus_index_in_session(self):
        session = AgentSession()
        assert focus_index_in(_HITS, session) is None
        session.set("last_focus_product_id", "p2")
        assert focus_index_in(_HITS, session) == 1

    def test_resolve_one_relative(self, monkeypatch):
        _no_llm(monkeypatch)
        assert resolve_one("上一个呢", _HITS, focus_index=1) == 0
        assert resolve_one("上一个呢", _HITS) is None


class TestAttributes:
    def test_color_matches_only_products_with_that_sku(self):
        assert resolve_by_name("黑色那个", _HITS) == [0]
        assert resolve_by_name("红色的那款", _HITS) == [2]
        # 两款都有白色 → 两个命中，交 LLM / 反问
        assert resolve_by_name("白色那个", _HITS) == [0, 1]

    def test_color_stem_matches_fancy_color_names(self):
        hits = [
            {"product_id": "a", "title": "小米 15", "options": {"颜色": ["星芒黑", "雪域白"], "存储": ["256GB", "512GB"]}},
            {"product_id": "b", "title": "iPhone 17", "options": {"颜色": ["宇宙橙 Cosmic Orange", "深空黑"]}},
            {"product_id": "c", "title": "MacBook", "options": {"机身颜色": ["深空灰", "银色"]}},
        ]
        assert resolve_by_name("黑色那个", hits) == [0, 1]
        assert resolve_by_name("橙色的", hits) == [1]
        assert resolve_by_name("灰的那款", hits) == [2]
        assert resolve_by_name("银色那个", hits) == [2]
        # 「小米」里的「米」不是颜色：没有 色/的/款 跟随，不触发
        assert resolve_by_name("小米那款", hits) == [0]
        # 整值命中：存储容量
        assert resolve_by_name("512 GB 的那个", hits) == [0]

    def test_name_beats_attribute_when_both_present(self):
        # 四款都有 42 码，但只点名了阿迪达斯 → 只命中阿迪达斯
        hits = [dict(h, options={"尺码": ["41", "42码"]}) for h in _HITS]
        assert resolve_by_name("把阿迪达斯 42码的加入购物车", hits) == [1]
        # 点名了但规格对不上（耐克没有红色）→ 仍以点名为准
        assert resolve_by_name("耐克红色那个", _HITS) == [0]
        # 没点名、但四款都有 42 码 → 没有区分度，不指向任何一款（交给焦点商品）
        assert resolve_by_name("42码的那个", hits) == []
        # 只有部分候选有该规格 → 指向它们
        hits[2]["options"] = {"尺码": ["43码"]}
        assert resolve_by_name("42码的那个", hits) == [0, 1]

    def test_generic_category_word_does_not_name_a_product(self):
        hits = [
            {"product_id": "a", "title": "OPPO Find X9 Ultra 影像旗舰", "options": {"颜色": ["星芒黑"]}},
            {"product_id": "b", "title": "小米 17 Ultra 2K 高刷 5G手机", "options": {"颜色": ["雪域白"]}},
        ]
        # 「手机」是品类词，不算点名小米；「黑色」才是信号 → OPPO
        assert resolve_by_name("将黑色款手机加入购物车", hits) == [0]

    def test_single_char_values_are_ignored(self):
        hits = [{"product_id": "x", "title": "T恤", "options": {"尺码": ["M", "L"]}}]
        assert resolve_by_name("M 的", hits) == []

    def test_resolve_one_picks_by_color_without_llm(self, monkeypatch):
        _no_llm(monkeypatch)
        assert resolve_one("黑色那个加购物车", _HITS) == 0
        assert resolve_one("白色那个", _HITS) is None  # 歧义，LLM 禁用 → 反问

    def test_enrich_candidates_collects_varying_sku_values(self):
        sku = lambda **props: SimpleNamespace(properties=props)  # noqa: E731
        detail = SimpleNamespace(
            title="耐克跑鞋", brand="耐克", category="服饰运动", sub_category="跑步鞋",
            skus=[sku(颜色="黑色", 尺码="41", 版本="标准版"), sku(颜色="白色", 尺码="41", 版本="标准版")],
        )
        store = SimpleNamespace(get_product_detail=lambda pid: detail)
        out = enrich_candidates([{"product_id": "p1"}], store)
        assert out[0]["title"] == "耐克跑鞋"
        assert out[0]["sub_category"] == "跑步鞋"
        # 单值维度也保留（一款只有一个颜色时，它正是区分不同商品的依据）
        assert out[0]["options"] == {"颜色": ["黑色", "白色"], "尺码": ["41"], "版本": ["标准版"]}

    def test_enrich_survives_store_failure(self):
        def boom(pid):
            raise RuntimeError("db down")

        out = enrich_candidates([{"product_id": "p1", "title": "T"}], SimpleNamespace(get_product_detail=boom))
        assert out == [{"product_id": "p1", "title": "T"}]

    def test_llm_prompt_lists_options(self):
        from agent.tools.llm_match import _format_candidates

        text = _format_candidates(_HITS[:1])
        assert "可选规格：颜色=黑色/白色；尺码=41/42" in text
