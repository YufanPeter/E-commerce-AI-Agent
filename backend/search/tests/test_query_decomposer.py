"""query_decomposer 单元测试：不依赖网络，用 fake 响应覆盖核心分支。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from search.query_decomposer import (
    MAX_SUB_REQUESTS,
    SubRequest,
    _normalize,
    decompose_query,
)


def _fake_response(requests: list[dict[str, str]]) -> Any:
    """模拟 Ark function-calling 返回 split_shopping_requests 的最小骨架。"""
    args = json.dumps({"requests": requests})
    tool_call = SimpleNamespace(function=SimpleNamespace(arguments=args))
    msg = SimpleNamespace(tool_calls=[tool_call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class TestNormalize:
    def test_dedup_and_strip(self):
        out = _normalize(
            [
                {"label": " 防晒 ", "query": "三亚 防晒"},
                {"label": "防晒2", "query": "三亚 防晒"},   # 重复 query，丢弃
                {"label": "衣服", "query": "三亚 速干衣"},
            ],
            original_query="x",
        )
        assert [s.label for s in out] == ["防晒", "衣服"]

    def test_query_falls_back_to_label(self):
        out = _normalize([{"label": "充电宝", "query": ""}], original_query="x")
        assert out == [SubRequest(label="充电宝", query="充电宝")]

    def test_skips_empty(self):
        out = _normalize([{"label": "", "query": ""}, {}], original_query="x")
        assert out == []

    def test_truncates_to_max(self):
        many = [{"label": f"c{i}", "query": f"q{i}"} for i in range(MAX_SUB_REQUESTS + 3)]
        out = _normalize(many, original_query="x")
        assert len(out) == MAX_SUB_REQUESTS


class TestDecomposeQuery:
    def test_multi_intent(self):
        resp = _fake_response([
            {"label": "防晒", "query": "三亚海边 防晒霜"},
            {"label": "衣服", "query": "三亚夏季 速干衣"},
        ])
        with patch("search.query_decomposer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = resp
            out = decompose_query("去三亚旅游推荐衣服和防晒")
        assert [s.label for s in out] == ["防晒", "衣服"]

    def test_single_intent(self):
        resp = _fake_response([{"label": "洗面奶", "query": "油皮 洗面奶"}])
        with patch("search.query_decomposer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = resp
            out = decompose_query("推荐油皮洗面奶")
        assert len(out) == 1
        assert out[0].label == "洗面奶"

    def test_llm_failure_degrades_to_single(self):
        with patch("search.query_decomposer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.side_effect = RuntimeError("boom")
            out = decompose_query("推荐点东西")
        # 失败时退化为单需求 = 原 query
        assert out == [SubRequest(label="推荐点东西", query="推荐点东西")]

    def test_empty_requests_degrades_to_single(self):
        resp = _fake_response([])
        with patch("search.query_decomposer.get_client") as mock_client:
            mock_client.return_value.chat.completions.create.return_value = resp
            out = decompose_query("随便看看")
        assert out == [SubRequest(label="随便看看", query="随便看看")]
