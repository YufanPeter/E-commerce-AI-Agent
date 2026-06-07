from __future__ import annotations

"""多模态「拍照找货」的视觉能力封装。

两件事，都围绕"把一张图变成系统能检索的东西"：

1. ``vision_extract_query(image)``：用视觉大模型（VLM，复用现有 Ark 聊天 endpoint）
   把图片"看懂"成一句**中文检索 query**。这是图搜的主召回信号——把图像问题
   转成已经跑通的文本 RAG 问题，从而复用整条 search_service 链路。

2. ``embed_image(image)``：用现有多模态 embedding 接入点把图片编码成向量
   （与文本 embedding 同一个 2048 维空间），供"视觉相似"重排用。

``image`` 入参统一接受两种形式，与 Ark ``image_url`` 字段一致：
    - 远程 URL：``https://.../x.jpg``
    - 内联 base64：``data:image/jpeg;base64,/9j/4AAQ...``
前端 demo 走 base64 直传，无需对象存储中转。
"""

import logging
import os

from llm.client import get_client, get_embedding_client, get_model_id


logger = logging.getLogger(__name__)


# VLM 抽取检索 query 的系统提示：只描述商品本体，输出可直接检索的中文短语。
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

# 抽取失败/非商品图时的哨兵返回，调用方据此降级。
UNRECOGNIZED = "无法识别"


def _vision_timeout() -> float:
    return float(os.getenv("ARK_VISION_TIMEOUT", os.getenv("ARK_TIMEOUT", "30")))


def vision_extract_query(image: str) -> str:
    """让 VLM 看图，返回一句中文检索 query；无法识别时返回 ``UNRECOGNIZED``。

    image: 商品图片的远程 URL 或 ``data:image/...;base64,`` 内联串。
    """
    if not image or not image.strip():
        return UNRECOGNIZED

    client = get_client()
    response = client.chat.completions.create(
        model=get_model_id(),
        messages=[
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图里的商品是什么？给出检索关键词。"},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            },
        ],
        temperature=0.0,
        max_tokens=60,
        timeout=_vision_timeout(),
    )
    text = (response.choices[0].message.content or "").strip()
    # 模型偶尔会带上引号/句号，清掉，保留纯关键词。
    text = text.strip("「」\"'。．. \n\t")
    if not text or UNRECOGNIZED in text:
        logger.info("vision_extract_query: 无法识别商品图")
        return UNRECOGNIZED
    logger.info("vision_extract_query → %r", text)
    return text


def embed_image(image: str) -> list[float]:
    """把图片编码成向量（与文本 embedding 同一空间，2048 维）。

    复用现有多模态 embedding 接入点（``/embeddings/multimodal``）。
    image: 远程 URL 或 ``data:image/...;base64,`` 内联串。
    """
    client = get_embedding_client()
    model = os.getenv("ARK_EMBEDDING_MODEL", "doubao-embedding-text-240715")
    response = client.post(
        "/embeddings/multimodal",
        body={
            "model": model,
            "input": [{"type": "image_url", "image_url": {"url": image}}],
        },
        cast_to=object,
    )
    embedding = response["data"]["embedding"]
    return list(embedding)
