from __future__ import annotations

"""Shared Chroma connection and collection initialization."""

import os
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 必须在读取下面的 DASHSCOPE_EMBEDDING_MODEL 之前加载 .env：模块级 os.getenv 在导入时
# 立即求值，若此时 .env 还没加载，就会退回默认占位模型名，导致建库 / 检索 404。
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PERSIST_DIR = PROJECT_ROOT / "backend" / "storage" / "chroma"
DEFAULT_COLLECTION = "product_knowledge_dashscope"
FALLBACK_COLLECTION = "product_knowledge"
# 默认走阿里云百炼多模态 embedding API（云端推理），不再本地加载
# torch / sentence-transformers。文本和图片使用同一个模型与向量空间。
DEFAULT_EMBEDDING_MODEL = os.getenv(
    "DASHSCOPE_EMBEDDING_MODEL", "qwen3-vl-embedding"
)
FALLBACK_EMBEDDING_MODEL = os.getenv("ARK_EMBEDDING_MODEL", "")

# qwen3-vl-embedding 单次最多接受 20 个 content 元素。
_EMBEDDING_BATCH_SIZE = min(
    20, max(1, int(os.getenv("DASHSCOPE_EMBEDDING_BATCH_SIZE", "20")))
)


def _make_query_cache():
    from llm.cache import TTLCache

    return TTLCache(maxsize=1024)


_QUERY_EMBEDDING_CACHE = _make_query_cache()


class DashScopeEmbeddingFunction:
    """通过阿里云百炼多模态 embedding API 计算 Chroma 向量。

    Remote inference removes local PyTorch/sentence-transformers dependencies. Offline
    indexing and online retrieval share the same model to keep vector spaces consistent.

    qwen3-vl-embedding 支持批量文本，并与图片向量共享同一语义空间。更换
    模型或维度后必须重建向量库。
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBEDDING_MODEL,
        batch_size: int = _EMBEDDING_BATCH_SIZE,
    ) -> None:
        self._model = model
        self._batch_size = min(20, max(1, batch_size))

    # Chroma validates that this parameter is named ``input``.
    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        from llm.client import embed_multimodal_primary

        texts = [text if isinstance(text, str) else str(text) for text in input]
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(
                embed_multimodal_primary(
                    [{"text": text} for text in batch],
                    model=self._model,
                )
            )
        return vectors

    # Delegate query and document methods to the same implementation across Chroma versions.
    def embed_query(self, input: Sequence[str]) -> list[list[float]]:
        # 在线查询向量带 TTL 缓存：同一句检索词（热词 / 重试 / refine 承接）不必
        # 再打一次 embedding API，省下 ~300-800ms。
        texts = [t if isinstance(t, str) else str(t) for t in input]
        if len(texts) != 1:
            return self.__call__(texts)
        key = (self._model, texts[0])
        cached = _QUERY_EMBEDDING_CACHE.get(key)
        if cached is not None:
            return [cached]
        vectors = self.__call__(texts)
        if vectors:
            _QUERY_EMBEDDING_CACHE.set(key, vectors[0])
        return vectors

    def embed_documents(self, input: Sequence[str]) -> list[list[float]]:
        return self.__call__(input)

    def name(self) -> str:
        """供 Chroma 持久化 collection 配置时标识 embedding 来源。"""
        return "dashscope_qwen3_vl"


class ArkEmbeddingFunction:
    """原豆包向量函数，只用于查询保留的豆包备用索引。"""

    def __init__(self, model: str = FALLBACK_EMBEDDING_MODEL) -> None:
        self._model = model

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        from llm.client import embed_multimodal_ark

        texts = [text if isinstance(text, str) else str(text) for text in input]
        return embed_multimodal_ark([{"text": text} for text in texts])

    def embed_query(self, input: Sequence[str]) -> list[list[float]]:
        return self.__call__(input)

    def embed_documents(self, input: Sequence[str]) -> list[list[float]]:
        return self.__call__(input)

    def name(self) -> str:
        return "doubao_ark"


def create_collection(
    persist_dir: Path = DEFAULT_PERSIST_DIR,
    collection_name: str = DEFAULT_COLLECTION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    reset: bool = False,
    provider: str = "dashscope",
):
    """Open the configured Chroma collection, optionally clearing its old index.

    Offline indexing and runtime retrieval share this factory to keep collection name,
    persistence path, embedding model, and distance metric consistent.
    """
    try:
        import chromadb
    except ImportError as exc:
        raise SystemExit(
            "缺少依赖。请在仓库根目录运行："
            "source .venv/bin/activate && pip install -r backend/requirements.txt"
        ) from exc

    persist_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(persist_dir),
        settings=chromadb.Settings(anonymized_telemetry=False),
    )
    if reset:
        try:
            client.delete_collection(collection_name)
        except chromadb.errors.NotFoundError:
            pass

    # Chroma 会用这个函数计算离线 chunk 向量和在线 query 向量。
    if provider == "dashscope":
        embedding_function = DashScopeEmbeddingFunction(model=embedding_model)
    elif provider == "ark":
        embedding_function = ArkEmbeddingFunction(model=embedding_model)
    else:
        raise ValueError(f"不支持的 embedding provider：{provider}")
    return client.get_or_create_collection(
        name=collection_name,
        embedding_function=embedding_function,
        metadata={"hnsw:space": "cosine"},
    )
