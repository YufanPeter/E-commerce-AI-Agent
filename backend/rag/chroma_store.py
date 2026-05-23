from __future__ import annotations

"""Chroma 连接与 collection 初始化的共享入口。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PERSIST_DIR = PROJECT_ROOT / "backend" / "storage" / "chroma"
DEFAULT_COLLECTION = "product_knowledge"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"


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
        from chromadb.utils import embedding_functions
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
    embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=embedding_model
    )
    return client.get_or_create_collection(
        name=collection_name,
        embedding_function=embedding_function,
        metadata={"hnsw:space": "cosine"},
    )
