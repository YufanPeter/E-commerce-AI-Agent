"""共享测试夹具与测试替身（fakes）。

约定：
- 单元测试用 FakeRetriever / FakeReranker，不加载任何模型、不连 Chroma、不调 LLM，
  保证快速、确定性。
- 集成测试（@pytest.mark.integration）用真实 Chroma + CrossEncoder，但仍注入
  ParsedQuery 绕开线上 LLM（豆包 key 可能失效），只验证检索+精排+fallback 链路。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 让 store / search / rag / llm 这些顶层包可被导入（与后端运行约定一致：以 backend/ 为根）。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 集成测试走本地模型缓存，避免联网 HF Hub 抖动。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from rag.reranker import RerankedChunk  # noqa: E402
from rag.retriever import RetrievedChunk  # noqa: E402
from search.query_understanding import ParsedQuery  # noqa: E402
from store.product_store import ProductStore  # noqa: E402


# ---------------------------------------------------------------------------
# 固定 demo 数据中的已知商品（断言锚点，数据变更时同步更新）
# ---------------------------------------------------------------------------

NIKE_RUNNING_SHOE = "p_clothes_007"          # 耐克 跑步鞋 base 899
HIKING_SHOES = ("p_clothes_015", "p_clothes_014")  # 迈乐 1099 / 萨洛蒙 1198（无始祖鸟）
CHEAPEST_NON_APPLE_LAPTOP = "p_digital_004"  # 华为 6299（笔记本最低非苹果）
CLEANSER = "p_beauty_011"                    # 珊珂 洁面，SKU 52 / 69


@pytest.fixture(scope="session")
def store() -> ProductStore:
    return ProductStore()


@pytest.fixture
def make_parsed():
    """构造 ParsedQuery 的工厂，省去每个用例填全字段。"""

    def _make(**overrides) -> ParsedQuery:
        params = dict(original_query=overrides.pop("original_query", "test query"))
        params.update(overrides)
        return ParsedQuery(**params)

    return _make


class FakeRetriever:
    """确定性检索替身：按 where 过滤出 product_id，每个商品回一个 chunk。

    - where={"product_id": pid} 或 {"product_id": {"$in": [...]}}：按硬匹配 id 召回
    - where=None：全库召回（对应 fallback 的 full_vector 末级）
    documents 可为指定商品塞入自定义文案，用于否定成分过滤测试。
    """

    def __init__(self, product_ids, documents=None):
        self.product_ids = list(product_ids)
        self.documents = documents or {}
        self.calls: list[dict] = []

    def search(self, query, top_k=10, where=None):
        self.calls.append({"query": query, "top_k": top_k, "where": where})
        ids = self._select(where)
        return [
            RetrievedChunk(
                chunk_id=f"{pid}_chunk_{i}",
                document=self.documents.get(pid, f"{pid} 商品文案 评价"),
                metadata={"product_id": pid, "chunk_type": "official_faq"},
                distance=0.10 + i * 0.01,
            )
            for i, pid in enumerate(ids[:top_k])
        ]

    def _select(self, where):
        if not where:
            return list(self.product_ids)
        cond = where.get("product_id")
        if isinstance(cond, dict):
            wanted = set(cond.get("$in", []))
            return [p for p in self.product_ids if p in wanted]
        return [p for p in self.product_ids if p == cond]


class FakeReranker:
    """确定性精排替身：保持输入顺序，分数按顺序递减（第一个最高）。"""

    def rerank(self, query, chunks, top_k=None):
        chunk_list = list(chunks)
        out = [
            RerankedChunk(chunk=c, rerank_score=1.0 - i * 0.01)
            for i, c in enumerate(chunk_list)
        ]
        if top_k is not None:
            out = out[:top_k]
        return out
