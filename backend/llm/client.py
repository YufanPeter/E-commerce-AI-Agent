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
    return OpenAI(api_key=api_key, base_url=base_url)


def get_model_id() -> str:
    """返回当前默认模型 / endpoint id。"""
    _load_env_once()
    model = os.getenv("ARK_MODEL")
    if not model:
        raise RuntimeError("缺少 ARK_MODEL。请在 .env 中指定豆包 endpoint id。")
    return model
