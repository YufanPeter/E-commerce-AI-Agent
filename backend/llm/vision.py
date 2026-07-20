from __future__ import annotations

"""Visual capabilities for multimodal product search.

Two operations convert an image into searchable representations:

1. ``vision_extract_query`` uses a vision-language model to produce a Chinese retrieval
   query, allowing the existing text RAG pipeline to provide primary recall.

2. ``embed_image`` encodes the image in the same multimodal vector space used by text,
   providing a visual-similarity reranking signal.

``image`` accepts either a remote URL or an inline base64 data URL.
"""

import logging
import os

from llm.client import get_client, get_embedding_client, get_model_id


logger = logging.getLogger(__name__)


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
    return float(os.getenv("ARK_VISION_TIMEOUT", os.getenv("ARK_TIMEOUT", "30")))


def vision_extract_query(image: str) -> str:
    """Return a Chinese image-search query or ``UNRECOGNIZED``.

    ``image`` may be a remote URL or inline ``data:image/...;base64,`` value.
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
    # Remove occasional quotes and terminal punctuation around model output.
    text = text.strip("「」\"'。．. \n\t")
    if not text or UNRECOGNIZED in text:
        logger.info("vision_extract_query could not identify a product in the image")
        return UNRECOGNIZED
    logger.info("vision_extract_query → %r", text)
    return text


def embed_image(image: str) -> list[float]:
    """Encode an image into the multimodal embedding space.

    The configured multimodal endpoint is reused. ``image`` may be a remote URL or an
    inline base64 data URL.
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
