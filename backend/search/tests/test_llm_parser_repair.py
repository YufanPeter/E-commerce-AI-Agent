"""Regression tests for fault-tolerant `_loads_arguments` parsing.

Captures known malformed function-calling responses so future changes preserve layered recovery.
"""

import pytest

from search.llm_parser import _loads_arguments


def test_clean_json_passthrough():
    raw = '{"requests": [{"label": "鞋", "query": "跑鞋"}]}'
    assert _loads_arguments(raw) == {"requests": [{"label": "鞋", "query": "跑鞋"}]}


def test_code_fence_stripped():
    raw = '```json\n{"category": "数码电子"}\n```'
    assert _loads_arguments(raw) == {"category": "数码电子"}


def test_tool_call_protocol_leak_and_truncation():
    """Repair truncated JSON containing a protocol stop marker before its closing delimiters."""
    raw = (
        '{"requests": [{"label": "衣服", "query": "三亚夏季旅游 速干衣 短袖"}, '
        '{"label": "防晒", "query": "三亚海边旅游 防晒霜"}]\n</function>\n</seed:tool_call>'
    )
    assert _loads_arguments(raw) == {
        "requests": [
            {"label": "衣服", "query": "三亚夏季旅游 速干衣 短袖"},
            {"label": "防晒", "query": "三亚海边旅游 防晒霜"},
        ]
    }


def test_pure_truncation_missing_closers():
    """Balance missing outer object and array delimiters in purely truncated JSON."""
    raw = '{"requests": [{"label": "包", "query": "双肩包"}'
    assert _loads_arguments(raw) == {"requests": [{"label": "包", "query": "双肩包"}]}


def test_unrecoverable_raises():
    with pytest.raises(Exception):
        _loads_arguments("完全不是 JSON 的一段文字")
