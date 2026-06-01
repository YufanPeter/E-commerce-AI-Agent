from __future__ import annotations

"""豆包 Ark / OpenAI 兼容客户端的薄封装。

Ark 协议完全兼容 OpenAI SDK，因此直接复用 ``openai.OpenAI`` 即可。
本模块只做两件事：
1. 从环境变量加载 ARK_API_KEY / ARK_BASE_URL / ARK_MODEL；
2. 提供一个进程内单例客户端，避免重复创建连接。
"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_env_once() -> None:
    """从仓库根的 .env 加载配置，已存在的环境变量不会被覆盖。"""
    load_dotenv(PROJECT_ROOT / ".env", override=False)


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """进程内单例。首次调用时建立 HTTP 连接池。"""
    _load_env_once()
    api_key = os.getenv("ARK_API_KEY")
    base_url = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3/")
    if not api_key:
        raise RuntimeError(
            "缺少 ARK_API_KEY。请复制 .env.example 为 .env 并填入豆包 API Key。"
        )
    # 必须设置超时：OpenAI SDK 默认 600s，一旦 ARK 响应挂起会让整条检索链
    # 无限卡在 socket read 上（前端表现为一直「检索中」）。给一个合理上限并
    # 允许一次自动重试，让偶发网络抖动快速失败而非永久阻塞。
    timeout = float(os.getenv("ARK_TIMEOUT", "30"))
    max_retries = int(os.getenv("ARK_MAX_RETRIES", "1"))
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def get_model_id() -> str:
    """返回当前默认模型 / endpoint id。"""
    _load_env_once()
    model = os.getenv("ARK_MODEL")
    if not model:
        raise RuntimeError("缺少 ARK_MODEL。请在 .env 中指定豆包 endpoint id。")
    return model


@lru_cache(maxsize=1)
def get_embedding_client() -> OpenAI:
    """embedding 专用客户端单例。

    豆包 embedding 接入点通常与聊天模型使用不同的 API Key（甚至不同 base_url），
    所以单独维护一个客户端。配置回退顺序：
    - API Key：ARK_EMBEDDING_API_KEY → ARK_API_KEY
    - base_url：ARK_EMBEDDING_BASE_URL → ARK_BASE_URL → 默认 Ark 地址
    """
    _load_env_once()
    api_key = os.getenv("ARK_EMBEDDING_API_KEY") or os.getenv("ARK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 embedding 的 API Key。请在 .env 中设置 ARK_EMBEDDING_API_KEY"
            "（或复用 ARK_API_KEY）。"
        )
    base_url = (
        os.getenv("ARK_EMBEDDING_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or "https://ark.cn-beijing.volces.com/api/v3/"
    )
    timeout = float(os.getenv("ARK_TIMEOUT", "30"))
    max_retries = int(os.getenv("ARK_MAX_RETRIES", "1"))
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


@lru_cache(maxsize=1)
def get_rerank_client() -> OpenAI:
    """rerank 专用客户端单例。

    Rerank 走云端 API，避免后端镜像安装 sentence-transformers / torch / CUDA
    这类超大本地推理依赖。配置回退顺序：
    - API Key：ARK_RERANKING_API_KEY → ARK_RERANK_API_KEY → ARK_API_KEY
    - base_url：ARK_RERANKING_BASE_URL → ARK_RERANK_BASE_URL → ARK_BASE_URL → 默认 Ark 地址
    """
    _load_env_once()
    api_key = (
        os.getenv("ARK_RERANKING_API_KEY")
        or os.getenv("ARK_RERANK_API_KEY")
        or os.getenv("ARK_API_KEY")
    )
    if not api_key:
        raise RuntimeError(
            "缺少 rerank 的 API Key。请在 .env 中设置 ARK_RERANKING_API_KEY"
            "（或复用 ARK_API_KEY）。"
        )
    base_url = (
        os.getenv("ARK_RERANKING_BASE_URL")
        or os.getenv("ARK_RERANK_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or "https://ark.cn-beijing.volces.com/api/v3/"
    )
    timeout = float(os.getenv(
        "ARK_RERANKING_TIMEOUT",
        os.getenv("ARK_RERANK_TIMEOUT", os.getenv("ARK_TIMEOUT", "30")),
    ))
    max_retries = int(os.getenv("ARK_MAX_RETRIES", "1"))
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def get_rerank_model_id() -> str:
    """返回 rerank 模型 / endpoint id。"""
    _load_env_once()
    model = os.getenv("ARK_RERANKING_MODEL") or os.getenv("ARK_RERANK_MODEL")
    if not model:
        raise RuntimeError("缺少 ARK_RERANKING_MODEL。未配置时请设置 USE_RERANK=0。")
    return model
