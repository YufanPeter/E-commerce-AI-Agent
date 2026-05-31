from __future__ import annotations

"""CrossEncoder 精排器。

为什么需要它：
    Chroma 用的是 bi-encoder（query 和 doc 分别编码再算余弦），快但粗。
    它能告诉你"这两个文本主题相近"，但分不清"哪个真正回答了 query"。
    CrossEncoder 把 query 和 doc 拼起来一次性喂给模型，能理解上下文关系，
    精度高得多但慢得多 —— 所以只对 bi-encoder 召回的 top-N 用它精排。

模型选择：BAAI/bge-reranker-base
    - 与现有 embedding（bge-small-zh-v1.5）同家族，对中文电商语料友好
    - ~280MB，CPU 单条 ~10ms，50 条 batch 一次 <500ms，满足实时要求
    - 输出 logit 分数（无固定范围），但同一 query 内可比较 → 排序用足够
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from rag.retriever import RetrievedChunk


logger = logging.getLogger(__name__)

DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base"


@dataclass(frozen=True)
class RerankedChunk:
    """带 rerank 分数的 chunk，保留原 chunk 全部信息。

    用 dataclass 包一层而不是直接给 RetrievedChunk 加字段，原因：
    - RetrievedChunk 是 Chroma 层的契约，rerank 是上层增强，不该污染下层
    - frozen dataclass 之间用 replace 可以无副作用地携带新字段
    """
    chunk: RetrievedChunk
    rerank_score: float

    @property
    def product_id(self) -> str:
        return self.chunk.product_id

    @property
    def chunk_type(self) -> str:
        return self.chunk.chunk_type


class CrossEncoderReranker:
    """对 (query, chunk_doc) pair 用 CrossEncoder 算精排分数。"""

    def __init__(self, model_name: str = DEFAULT_RERANKER_MODEL) -> None:
        # 延迟导入：sentence_transformers 首次 import 会触发 torch 初始化，~1s。
        # 放到 __init__ 里，让纯 import 这个模块的代码（如测试）不付这个成本。
        from sentence_transformers import CrossEncoder

        logger.info("Loading CrossEncoder model: %s", model_name)
        self._model = CrossEncoder(model_name)

    def rerank(
        self,
        query: str,
        chunks: Iterable[RetrievedChunk],
        top_k: int | None = None,
    ) -> list[RerankedChunk]:
        """对 chunks 按 query 相关性重新排序。

        top_k: 只保留前 K 个，None 表示全部返回。

        返回按 rerank_score 降序排列。空输入直接返回空列表，避免模型空 batch 报错。
        """
        chunk_list = list(chunks)
        if not chunk_list:
            return []

        pairs = [(query, c.document) for c in chunk_list]
        scores = self._model.predict(pairs)

        reranked = [
            RerankedChunk(chunk=c, rerank_score=float(s))
            for c, s in zip(chunk_list, scores)
        ]
        reranked.sort(key=lambda r: r.rerank_score, reverse=True)

        if top_k is not None:
            reranked = reranked[:top_k]
        return reranked


@lru_cache(maxsize=1)
def get_reranker() -> CrossEncoderReranker:
    """进程级单例，避免重复加载模型。"""
    return CrossEncoderReranker()
