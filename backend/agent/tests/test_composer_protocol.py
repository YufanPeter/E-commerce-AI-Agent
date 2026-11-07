"""composer 行协议：增量解析、结构化事件顺序、JSON 兜底、缓存。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.composer import (
    AnswerComposer,
    _ProtocolStreamParser,
    _protocol_text_to_json,
    clear_narrative_cache,
)
from agent.session import AgentSession
from agent.tools.base import ToolResult


def _chunks(pieces):
    for p in pieces:
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=p))])


def _recommend(hits=None):
    return ToolResult(
        tool_name="recommend",
        payload={"query": "耳机", "products": hits or [
            {"product_id": "p1", "title": "A 耳机", "price": 199},
            {"product_id": "p2", "title": "B 耳机", "price": 299},
        ]},
        composer_hint="h",
    )


class TestParser:
    def test_opening_streams_incrementally_then_items_and_followups(self):
        parser = _ProtocolStreamParser()
        events = []
        for piece in ["开场", "｜为你", "推荐了两款", "耳机\n商品｜p1｜轻巧", "舒适\n商品｜p2｜降噪强\n追问｜便宜点的\n追问｜对比一下"]:
            events.extend(parser.feed(piece))
        events.extend(parser.finish())
        parts = [e["part"] for e in events]
        assert parts == ["opening", "opening", "opening", "opening_end", "item", "item", "followup", "followup"]
        assert "".join(e["delta"] for e in events if e["part"] == "opening") == "为你推荐了两款耳机"
        assert events[3]["text"] == "为你推荐了两款耳机"
        assert events[4] == {"part": "item", "index": 0, "productId": "p1", "description": "轻巧舒适"}
        assert events[5]["productId"] == "p2"
        assert [e["text"] for e in events if e["part"] == "followup"] == ["便宜点的", "对比一下"]
        assert json.loads(parser.to_json()) == {
            "opening": "为你推荐了两款耳机",
            "items": [{"productId": "p1", "description": "轻巧舒适"}, {"productId": "p2", "description": "降噪强"}],
            "followup": ["便宜点的", "对比一下"],
        }

    def test_tolerates_half_width_pipe_and_list_prefix(self):
        out = _protocol_text_to_json("- 开场|你好\n1. 商品|p1|不错\n追问|再看看")
        assert json.loads(out) == {"opening": "你好", "items": [{"productId": "p1", "description": "不错"}], "followup": ["再看看"]}

    def test_non_protocol_text_returns_none(self):
        assert _protocol_text_to_json("随便说一句") is None
        assert _protocol_text_to_json('{"opening": "x", "items": []}') is None

    def test_duplicate_items_and_followups_are_dropped(self):
        parser = _ProtocolStreamParser()
        parser.feed("开场｜a\n商品｜p1｜x\n商品｜p1｜y\n追问｜q\n追问｜q\n")
        assert len(parser.items) == 1
        assert parser.followup == ["q"]


class TestComposeEvents:
    def test_events_then_final_json(self):
        with patch("agent.composer.get_client") as mc:
            mc.return_value.chat.completions.create.return_value = _chunks(
                ["开场｜为你推荐了两款\n", "商品｜p1｜轻巧\n商品｜p2｜降噪\n", "追问｜便宜点"]
            )
            events = list(AnswerComposer().compose_events(_recommend(), AgentSession()))
        types = [e["type"] for e in events]
        assert types[-1] == "final"
        assert all(t == "narrative" for t in types[:-1])
        parts = [e["data"]["part"] for e in events[:-1]]
        assert parts == ["opening", "opening_end", "item", "item", "followup"]
        final = json.loads(events[-1]["data"])
        assert final["opening"] == "为你推荐了两款"
        assert [i["productId"] for i in final["items"]] == ["p1", "p2"]
        assert final["followup"] == ["便宜点"]

    def test_json_output_is_still_accepted(self):
        text = '{"opening":"o","items":[{"productId":"p1","description":"d"}],"followup":["f"]}'
        with patch("agent.composer.get_client") as mc:
            mc.return_value.chat.completions.create.return_value = _chunks([text])
            events = list(AnswerComposer().compose_events(_recommend(), AgentSession()))
        parts = [e["data"]["part"] for e in events if e["type"] == "narrative"]
        assert parts == ["opening", "opening_end", "item", "followup"]
        assert json.loads(events[-1]["data"])["opening"] == "o"

    def test_plain_text_tool_streams_tokens(self):
        sr = ToolResult(tool_name="product_detail", payload={"query": "q", "product": {"title": "T"}}, composer_hint="h")
        with patch("agent.composer.get_client") as mc:
            mc.return_value.chat.completions.create.return_value = _chunks(["这款", "不错"])
            events = list(AnswerComposer().compose_events(sr, AgentSession()))
        assert [e["type"] for e in events] == ["token", "token", "final"]
        assert events[-1]["data"] == "这款不错"

    def test_mid_flight_error_keeps_partial_and_adds_followups(self):
        def gen():
            yield from _chunks(["开场｜为你推荐\n商品｜p1｜好\n"])
            raise RuntimeError("boom")

        with patch("agent.composer.get_client") as mc:
            mc.return_value.chat.completions.create.return_value = gen()
            events = list(AnswerComposer().compose_events(_recommend(), AgentSession()))
        final = json.loads(events[-1]["data"])
        assert final["opening"] == "为你推荐"
        assert final["items"] == [{"productId": "p1", "description": "好"}]
        assert final["followup"]

    def test_cache_hit_skips_llm_and_replays_events(self):
        clear_narrative_cache()
        with patch("agent.composer.get_client") as mc:
            mc.return_value.chat.completions.create.return_value = _chunks(["开场｜o\n商品｜p1｜d\n追问｜f"])
            first = list(AnswerComposer().compose_events(_recommend(), AgentSession()))
            second = list(AnswerComposer().compose_events(_recommend(), AgentSession()))
            assert mc.return_value.chat.completions.create.call_count == 1
        assert first[-1]["data"] == second[-1]["data"]
        assert [e["data"]["part"] for e in second[:-1]] == ["opening", "opening_end", "item", "followup"]

    def test_compose_blocking_folds_protocol_into_json(self):
        resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="开场｜o\n商品｜p1｜d\n追问｜f", tool_calls=None))])
        with patch("agent.composer.get_client") as mc:
            mc.return_value.chat.completions.create.return_value = resp
            out = AnswerComposer().compose(_recommend(), AgentSession())
        assert json.loads(out) == {"opening": "o", "items": [{"productId": "p1", "description": "d"}], "followup": ["f"]}


class TestSectionsAndSummary:
    TEXT = (
        "开场｜为你推荐了三款\n"
        "分组｜通勤降噪\n"
        "商品｜p1｜降噪强\n"
        "商品｜p2｜续航长\n"
        "分组｜运动防水\n"
        "商品｜p3｜不易掉\n"
        "总结｜通勤选 A，运动选 C。你更常在哪儿用？\n"
        "追问｜便宜点的\n"
    )

    def test_items_carry_their_section_and_summary_comes_before_followups(self):
        parser = _ProtocolStreamParser()
        events = parser.feed(self.TEXT) + parser.finish()
        parts = [e["part"] for e in events]
        assert parts == ["opening", "opening_end", "group", "item", "item", "group", "item", "summary", "followup"]
        assert [e.get("group") for e in events if e["part"] == "item"] == ["通勤降噪", "通勤降噪", "运动防水"]
        data = json.loads(parser.to_json())
        assert data["summary"] == "通勤选 A，运动选 C。你更常在哪儿用？"
        assert data["items"][2] == {"productId": "p3", "description": "不易掉", "group": "运动防水"}

    def test_repeated_section_line_and_second_summary_are_dropped(self):
        parser = _ProtocolStreamParser()
        events = parser.feed("开场｜a\n分组｜x\n分组｜x\n商品｜p1｜y\n总结｜一\n总结｜二\n") + parser.finish()
        assert [e["part"] for e in events].count("group") == 1
        assert parser.summary == "一"

    def test_cache_hit_replays_sections_and_summary_in_the_same_order(self):
        with patch("agent.composer.get_client") as client:
            client.return_value.chat.completions.create.side_effect = lambda **kw: _chunks([self.TEXT])
            composer = AnswerComposer()
            first = list(composer.compose_events(_recommend(), AgentSession()))
            second = list(composer.compose_events(_recommend(), AgentSession()))
        assert client.return_value.chat.completions.create.call_count == 1

        def parts(events):
            return [e["data"]["part"] for e in events if e["type"] == "narrative" and e["data"]["part"] != "opening"]

        assert parts(first) == parts(second)
        assert json.loads(first[-1]["data"]) == json.loads(second[-1]["data"])

    def test_model_answering_in_json_keeps_sections_and_summary(self):
        text = json.dumps({
            "opening": "好",
            "items": [{"productId": "p1", "description": "x", "group": "组"}],
            "followup": [],
            "summary": "选它",
        }, ensure_ascii=False)
        with patch("agent.composer.get_client") as client:
            client.return_value.chat.completions.create.return_value = _chunks([text])
            events = list(AnswerComposer().compose_events(_recommend(), AgentSession()))
        data = [e["data"] for e in events if e["type"] == "narrative"]
        assert {"part": "group", "label": "组"} in data
        assert {"part": "summary", "text": "选它"} in data
