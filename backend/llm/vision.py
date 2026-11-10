from __future__ import annotations

"""Visual capabilities for multimodal product search.

Two operations convert an image into searchable representations:

1. ``vision_extract_query(image)``：用多模态对话模型（DeepSeek deepseek-flash /
   百炼 Qwen-VL，由 LLM_PROVIDER 决定）把图片"看懂"成一句**中文检索 query**。这是图搜的主召回信号——把图像问题
   转成已经跑通的文本 RAG 问题，从而复用整条 search_service 链路。

2. ``embed_image(image)``：用百炼多模态 embedding 把图片编码成向量
   （与文本 embedding 同一个 2048 维空间），供"视觉相似"重排用。

``image`` 入参统一接受两种形式，与 OpenAI 兼容 ``image_url`` 字段一致：
    - 远程 URL：``https://.../x.jpg``
    - 内联 base64：``data:image/jpeg;base64,/9j/4AAQ...``
前端 demo 走 base64 直传，无需对象存储中转。
"""

import logging
import os

from llm.client import (
    embed_multimodal_primary,
    embed_multimodal_with_provider,
    get_embedding_model_id,
    get_client,
    get_vision_model_id,
)


logger = logging.getLogger(__name__)
from i18n import en as english
from i18n import is_english, t


# System prompt for extracting a directly searchable Chinese product phrase.
_EXTRACT_SYSTEM = (
    "你是电商导购的视觉识别助手。用户给你一张商品图片，"
    "你要输出一句**中文商品检索关键词**，让系统据此在商品库里找同类商品。\n"
    "要求：\n"
    "1. 只描述图中的【商品本体】：品类、子品类、颜色、材质、风格、明显可见的品牌；"
    "忽略背景、人物、手、桌面等无关元素。\n"
    "2. 输出一句自然的中文检索短语，像用户会怎么搜，例如：黑色男士纯色圆领短袖T恤、"
    "白色无线蓝牙降噪耳机、粉色保湿精华护肤品。\n"
    "3. 不要输出解释、不要加引号、不要分点，只输出这一句关键词。\n"
    "4. 如果图里看不清商品或不是商品图，只输出两个字：无法识别。"
)

# Sentinel returned for extraction failure or a non-product image.
UNRECOGNIZED = "无法识别"


def _vision_timeout() -> float:
    return float(
        os.getenv(
            "LLM_VISION_TIMEOUT",
            os.getenv("DASHSCOPE_VISION_TIMEOUT", os.getenv("DASHSCOPE_TIMEOUT", "30")),
        )
    )


def vision_extract_query(image: str) -> str:
    """Return a Chinese image-search query or ``UNRECOGNIZED``.

    ``image`` may be a remote URL or inline ``data:image/...;base64,`` value.
    """
    if not image or not image.strip():
        return UNRECOGNIZED

    client = get_client()
    response = client.chat.completions.create(
        model=get_vision_model_id(),
        messages=[
            {"role": "system", "content": english.VISION_SYSTEM if is_english() else _EXTRACT_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": t("vision.user")},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            },
        ],
        temperature=0.0,
        max_tokens=60,
        timeout=_vision_timeout(),
    )
    text = (response.choices[0].message.content or "").strip()
    # Remove occasional quotes and terminal punctuation around model output.
    text = text.strip("「」\"'。．. \n\t")
    if not text or UNRECOGNIZED in text or text.upper() == "UNRECOGNIZED":
        logger.info("vision_extract_query could not identify a product in the image")
        return UNRECOGNIZED
    logger.info("vision_extract_query → %r", text)
    return text


def parsed_query_from_phrase(phrase: str):
    """把 VLM 抽出的检索短语确定性地变成 ParsedQuery，图搜据此跳过 LLM query 解析。

    图搜的硬过滤只需要品类：在短语里按"最长匹配"找库里真实存在的子类目名
    （如“精华液”命中“精华”、“跑步鞋”命中“跑步鞋”），再反查一级类目。价格 / 品牌 /
    否定这些约束在纯图搜里本来就不存在，不值得为它们再花一次 2s 的 LLM 往返。
    """
    from search.query_understanding import ParsedQuery, load_taxonomy

    text = (phrase or "").strip()
    sub_to_cat = load_taxonomy()["sub_to_cat"]
    sub = None
    for candidate in sorted(sub_to_cat, key=len, reverse=True):
        if candidate and candidate in text:
            sub = candidate
            break
    return ParsedQuery(
        original_query=text,
        intent="product_search",
        category=sub_to_cat.get(sub) if sub else None,
        sub_category=sub,
        retrieval_query=text,
        needs_clarification=False,
    )


def embed_image(image: str) -> list[float]:
    """把图片编码成向量（与文本 embedding 同一空间，默认 2048 维）。

    image: 远程 URL 或 ``data:image/...;base64,`` 内联串。
    """
    _, vectors = embed_multimodal_with_provider(
        [{"image": image}],
        model=get_embedding_model_id(),
    )
    return vectors[0]


def embed_image_with_provider(image: str) -> tuple[str, list[float]]:
    """编码图片并返回实际 provider，供调用方选择同空间的视觉索引。"""
    provider, vectors = embed_multimodal_with_provider(
        [{"image": image}],
        model=get_embedding_model_id(),
    )
    return provider, vectors[0]


def embed_image_primary(image: str) -> list[float]:
    """只用百炼编码图片，供构建百炼视觉索引，禁止混入豆包向量。"""
    return embed_multimodal_primary(
        [{"image": image}],
        model=get_embedding_model_id(),
    )[0]
