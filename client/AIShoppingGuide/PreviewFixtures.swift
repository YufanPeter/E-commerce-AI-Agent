import Foundation

#if DEBUG
enum PreviewFixtures {
    static let sunscreenJSON = """
    {
        "opening": "为你推荐了3款口碑不错的高倍防晒好物",
        "items": [
            {"productId": "p_beauty_023", "description": "理肤泉特护清盈防晒乳：专为易敏肌设计，质地清爽控油"},
            {"productId": "p_beauty_010", "description": "安热沙金灿倍护防晒乳：防水防汗，户外暴晒首选"},
            {"productId": "p_beauty_006", "description": "欧莱雅多重防护隔离露：水感轻薄还带提亮，伪素颜很方便"}
        ],
        "questions": ["需要更平价的防晒选择？", "想要专为敏感肌定制的防晒款？"]
    }
    """

    static var recommendMessage: ChatMessage {
        ChatMessage(
            sender: .ai,
            text: sunscreenJSON,
            state: .ready,
            products: Array(Product.samples.prefix(3))
        )
    }

    static var loadingMessage: ChatMessage {
        ChatMessage(sender: .ai, text: "正在检索商品", state: .retrieving)
    }
}
#endif
