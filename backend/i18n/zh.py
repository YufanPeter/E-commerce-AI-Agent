"""Chinese user-facing copy for the demo catalog."""

MESSAGES: dict[str, str] = {
    "status.thinking": "思考中…",
    "status.vision": "正在识别图片…",
    "status.visual_search": "正在按图找相似商品…",
    "image.display": "[图片搜索]",
    "image.display_hint": "[图片] {hint}",
    "image.unrecognized": "没看清这张图里的商品呢～换张更清晰的图，或者用文字描述一下你想找什么？",
    "review.title": "{nickname} · {rating} 星",
    "memory.removed": "已忘掉这条",
    "memory.restored": "已恢复",
    "home.keep_browsing": "接着看{label}",
    "home.browse_query": "推荐{label}",
    "vision.user": "这张图里的商品是什么？给出检索关键词。",
    "clarify.prompt": (
        "为了帮你更精准地推荐，可以补充一下：\n"
        "1. 想看哪个品类？（美妆护肤 / 数码电子 / 服饰运动 / 食品饮料）\n"
        "2. 预算大概多少？\n"
        "3. 用途或偏好？（例如：敏感肌、跑步、办公便携 等）"
    ),
    "fallback.prompt": (
        "抱歉，我是电商导购助手，专门帮你挑选商品。"
        "你可以告诉我想买的品类、预算或使用场景，我会给出推荐～"
    ),
    "cart.unavailable": "当前商品库暂不支持加入购物车。",
}
