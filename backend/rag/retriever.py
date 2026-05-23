from __future__ import annotations

"""运行时 Chroma 检索器，供后续 API 服务复用。

FastAPI 等常驻服务应在启动时创建一个 ``ChromaRetriever``，之后所有用户
请求复用同一个实例，让 Chroma client 和 embedding 模型保持热状态。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag.chroma_store import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_PERSIST_DIR,
    create_collection,
)


@dataclass(frozen=True)
class RetrievedChunk:
    """一次 Chroma 命中的标准化结果，便于后续排序和聚合。"""

    chunk_id: str
    document: str
    metadata: dict[str, Any]
    distance: float | None

    @property
    def product_id(self) -> str:
        return str(self.metadata.get("product_id", ""))

    @property
    def chunk_type(self) -> str:
        return str(self.metadata.get("chunk_type", ""))


class ChromaRetriever:
    """对已有商品知识向量库的轻量运行时封装。"""

    def __init__(
        self,
        persist_dir: Path = DEFAULT_PERSIST_DIR,
        collection_name: str = DEFAULT_COLLECTION,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        """加载 collection 和 embedding 模型，用于后续重复检索。"""
        self.collection = create_collection(
            persist_dir=persist_dir,
            collection_name=collection_name,
            embedding_model=embedding_model,
            reset=False,
        )

    def count(self) -> int:
        """返回当前 collection 中的 chunk 数量。"""
        return self.collection.count()

    def search(
        self,
        query: str,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """根据用户 query 检索语义证据 chunk。

        ``where`` 是 Chroma metadata 过滤器，适合做类目、product_id 等粗过滤。
        价格、SKU、品牌排除等硬业务约束后续应由 SQLite Product Store 兜底。
        """
        result = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return self._parse_result(result)

    def _parse_result(self, result: dict[str, Any]) -> list[RetrievedChunk]:
        """把 Chroma 嵌套返回结构转换成更好用的结果对象。"""
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]

        chunks: list[RetrievedChunk] = []
        for index, chunk_id in enumerate(ids):
            distance = distances[index] if index < len(distances) else None
            chunks.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    document=documents[index],
                    metadata=metadatas[index],
                    distance=distance,
                )
            )
        return chunks
