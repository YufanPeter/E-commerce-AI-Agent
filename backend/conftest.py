"""测试全局夹具：隔离进程内 LLM 缓存，并关闭后台预取（预取会绕过 mock 真打 LLM）。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_caches(monkeypatch):
    monkeypatch.setenv("LLM_PREFETCH", "0")
    monkeypatch.setenv("MEMORY_LLM_EXTRACT", "0")
    from agent import composer, intent_router
    from search import query_decomposer

    intent_router.clear_route_cache()
    composer.clear_narrative_cache()
    query_decomposer.clear_decompose_cache()
    yield
    intent_router.clear_route_cache()
    composer.clear_narrative_cache()
    query_decomposer.clear_decompose_cache()
