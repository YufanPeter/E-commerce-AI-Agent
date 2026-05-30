from __future__ import annotations

"""Chroma 连接与 collection 初始化的共享入口。"""

import os
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 必须在读取下面的 ARK_EMBEDDING_MODEL 之前加载 .env：模块级 os.getenv 在导入时
# 立即求值，若此时 .env 还没加载，就会退回默认占位模型名，导致建库 / 检索 404。
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PERSIST_DIR = PROJECT_ROOT / "backend" / "storage" / "chroma"
DEFAULT_COLLECTION = "product_knowledge"
# 默认走豆包 Ark embedding API（云端推理），不再本地加载 torch / sentence-transformers。
# 具体 endpoint id / 模型名通过 .env 的 ARK_EMBEDDING_MODEL 覆盖。
DEFAULT_EMBEDDING_MODEL = os.getenv("ARK_EMBEDDING_MODEL", "doubao-embedding-text-240715")

# 豆包 embedding 单次请求的最大文本条数。超过会自动分批，避免触发服务端上限。
_EMBEDDING_BATCH_SIZE = int(os.getenv("ARK_EMBEDDING_BATCH_SIZE", "64"))


class DoubaoEmbeddingFunction:
    """通过豆包 Ark 多模态 embedding API 计算向量的 Chroma EmbeddingFunction。

    相比本地 ``SentenceTransformerEmbeddingFunction``，这里把 embedding 推理
    放到云端，因此进程无需 import torch / sentence-transformers，冷启动从
    分钟级降到秒级。离线建库和在线检索共用同一个 model，保证向量空间一致。

    使用的是 vision 系多模态接入点，调用 ``/embeddings/multimodal``：
    - 每次请求只接受单条 input，返回 ``data`` 为单个对象（非数组），因此逐条循环；
    - 输出维度与本地 bge 不同（如 2048），更换模型后必须用本函数重建向量库。
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBEDDING_MODEL,
        batch_size: int = _EMBEDDING_BATCH_SIZE,
    ) -> None:
        self._model = model
        self._batch_size = max(1, batch_size)
        # embedding 接入点的鉴权与聊天模型不同（独立 API Key / 可能独立 base_url），
        # 因此用专属客户端而非复用聊天单例。未配置独立 key 时回退到聊天 key。
        from llm.client import get_embedding_client

        self._client = get_embedding_client()

    def _embed_one(self, text: str) -> list[float]:
        """调用 multimodal 接口为单条文本取向量。"""
        response = self._client.post(
            "/embeddings/multimodal",
            body={
                "model": self._model,
                "input": [{"type": "text", "text": text}],
            },
            cast_to=object,
        )
        embedding = response["data"]["embedding"]
        return list(embedding)

    # 参数名必须是 input：chromadb 会校验 EmbeddingFunction.__call__ 的签名。
    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        texts = [text if isinstance(text, str) else str(text) for text in input]
        if not texts:
            return []
        if len(texts) == 1:
            return [self._embed_one(texts[0])]
        # multimodal 接口单请求只接受一条文本，离线建库有上千个 chunk，串行会很慢。
        # 用线程池并发发请求（受 IO 限制，线程足够），并按原始顺序回填结果，
        # 保证 ids / documents / embeddings 三者一一对应。
        from concurrent.futures import ThreadPoolExecutor

        max_workers = min(self._batch_size, len(texts))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(self._embed_one, texts))

    # chromadb 1.x 检索时调用 embed_query(input=...)；
    # 离线建库仍走 __call__（即 embed_documents）。两条路径输入输出形状一致，
    # 这里统一委托给 __call__，从而同时兼容 0.6.x 与 1.x。
    def embed_query(self, input: Sequence[str]) -> list[list[float]]:
        return self.__call__(input)

    def embed_documents(self, input: Sequence[str]) -> list[list[float]]:
        return self.__call__(input)

    def name(self) -> str:
        """供 Chroma 持久化 collection 配置时标识 embedding 来源。"""
        return "doubao_ark"


def create_collection(
    persist_dir: Path = DEFAULT_PERSIST_DIR,
    collection_name: str = DEFAULT_COLLECTION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    reset: bool = False,
):
    """打开配置好的 Chroma collection，必要时先清空旧索引。

    离线建库脚本和运行时检索器共用这个工厂函数，避免 collection 名称、
    持久化路径、embedding 模型和距离度量配置出现不一致。
    """
    try:
        import chromadb
    except ImportError as exc:
        raise SystemExit(
            "缺少依赖。请在仓库根目录运行："
            "source .venv/bin/activate && pip install -r backend/requirements.txt"
        ) from exc

    persist_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(persist_dir))
    if reset:
        try:
            client.delete_collection(collection_name)
        except chromadb.errors.NotFoundError:
            pass

    # Chroma 会用这个函数计算离线 chunk 向量和在线 query 向量。
    embedding_function = DoubaoEmbeddingFunction(model=embedding_model)
    return client.get_or_create_collection(
        name=collection_name,
        embedding_function=embedding_function,
        metadata={"hnsw:space": "cosine"},
    )
