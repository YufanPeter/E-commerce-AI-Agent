from __future__ import annotations

"""进程内 TTL 缓存 + 并发去重，给 LLM 调用层做"首字加速"。

为什么需要它：
    一次推荐请求里有 3~4 次串行 LLM 调用（router → decompose → query
    understanding → composer）。热门/重复 query（demo、回归、用户重试）
    每次都重新打一遍 LLM，首字延迟全部重来。这里提供两件小工具：

    1. ``TTLCache``：线程安全的 LRU + TTL。只缓存**纯函数式**的 LLM 结果
       （同输入 → 同输出可接受），例如首轮路由、query 解析、composer 话术。
    2. ``Deduper``：并发去重。orchestrator 会在 router 跑的同时**预取**
       query understanding；若 tool 阶段紧接着又要同一个结果，不该再打一次
       LLM，而是等那个在飞的 Future。

两者都只在进程内生效，单 worker 部署够用；多 worker 可换成 Redis。
"""

import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Generic, Hashable, TypeVar

T = TypeVar("T")

_MISSING = object()


def cache_ttl_seconds(default: float = 600.0) -> float:
    """LLM 结果缓存的 TTL（秒）。``LLM_CACHE_TTL=0`` 关闭全部缓存。"""
    raw = os.getenv("LLM_CACHE_TTL")
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


class TTLCache(Generic[T]):
    """线程安全的 LRU + TTL 缓存。``ttl<=0`` 时等价于关闭（永远 miss）。"""

    def __init__(self, maxsize: int = 256, ttl: float | None = None) -> None:
        self._maxsize = max(1, maxsize)
        self._ttl = cache_ttl_seconds() if ttl is None else ttl
        self._data: OrderedDict[Hashable, tuple[float, T]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: Hashable, default: Any = None) -> Any:
        if not self.enabled:
            self.misses += 1
            return default
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return default
            expires_at, value = item
            if expires_at < now:
                self._data.pop(key, None)
                self.misses += 1
                return default
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: Hashable, value: T) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class Deduper:
    """同 key 的并发调用只真正执行一次，其余等待同一个 Future。

    ``prefetch`` 在后台线程发起调用（不阻塞）；``run`` 若发现同 key 在飞就
    直接等它，否则在当前线程同步执行。执行完毕后 key 从在飞表移除——结果
    的持久缓存交给调用方（通常是 ``TTLCache`` / ``lru_cache``）。
    """

    def __init__(self, max_workers: int = 4, name: str = "llm-prefetch") -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)
        self._inflight: dict[Hashable, Future] = {}
        self._lock = threading.Lock()

    def prefetch(self, key: Hashable, fn: Callable[[], T]) -> Future:
        with self._lock:
            fut = self._inflight.get(key)
            if fut is not None:
                return fut
            fut = self._executor.submit(fn)
            self._inflight[key] = fut
        fut.add_done_callback(lambda _f, k=key: self._forget(k))
        return fut

    def run(self, key: Hashable, fn: Callable[[], T]) -> T:
        with self._lock:
            fut = self._inflight.get(key)
        if fut is not None:
            return fut.result()
        return fn()

    def _forget(self, key: Hashable) -> None:
        with self._lock:
            self._inflight.pop(key, None)

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)
