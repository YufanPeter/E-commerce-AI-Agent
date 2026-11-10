import Foundation

#if DEBUG
enum PreviewFixtures {
    static let sunscreenJSON = """
    {
        "opening": "I found three sunscreen options.",
        "items": [
            {"productId": "p_beauty_023", "description": "La Roche-Posay: a lightweight option."},
            {"productId": "p_beauty_010", "description": "Anessa: a water-resistant option."},
            {"productId": "p_beauty_006", "description": "L'Oréal: a light everyday option."}
        ],
        "followup": ["Show cheaper sunscreens", "Show sunscreen for sensitive skin"]
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
        ChatMessage(sender: .ai, text: "Searching products…", state: .retrieving)
    }
}
#endif
