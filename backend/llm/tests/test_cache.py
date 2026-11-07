from __future__ import annotations

import threading
import time

from llm.cache import Deduper, TTLCache


def test_ttl_cache_hit_and_expiry():
    cache: TTLCache[str] = TTLCache(maxsize=2, ttl=0.05)
    cache.set("a", "1")
    assert cache.get("a") == "1"
    time.sleep(0.08)
    assert cache.get("a") is None


def test_ttl_cache_lru_eviction():
    cache: TTLCache[int] = TTLCache(maxsize=2, ttl=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")          # a 变成最近使用
    cache.set("c", 3)       # 淘汰 b
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_ttl_cache_disabled_when_ttl_zero(monkeypatch):
    monkeypatch.setenv("LLM_CACHE_TTL", "0")
    cache: TTLCache[int] = TTLCache()
    cache.set("a", 1)
    assert cache.get("a") is None
    assert not cache.enabled


def test_deduper_runs_once_for_concurrent_same_key():
    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(1)
        return "v"

    d = Deduper(max_workers=2)
    fut = d.prefetch("k", slow)
    results = []
    t = threading.Thread(target=lambda: results.append(d.run("k", slow)))
    t.start()
    gate.set()
    t.join(2)
    assert fut.result(2) == "v"
    assert results == ["v"]
    assert len(calls) == 1
    time.sleep(0.05)
    assert d.inflight_count() == 0


def test_deduper_run_without_inflight_executes_inline():
    d = Deduper()
    assert d.run("x", lambda: 42) == 42
