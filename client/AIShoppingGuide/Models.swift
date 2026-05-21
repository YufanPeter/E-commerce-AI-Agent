import Foundation

enum Sender {
    case user
    case ai
}

enum MessageState: String {
    case understanding = "理解中"
    case retrieving = "检索中"
    case generating = "生成中"
    case ready
    case failed
}

struct Product: Identifiable, Hashable {
    let id = UUID()
    let title: String
    let price: String
    let reason: String
    let details: String
    let tags: [String]

    static let samples: [Product] = [
        Product(
            title: "温和氨基酸控油洗面奶",
            price: "¥129",
            reason: "适合油皮与混合肌，清洁力足够但洗后不紧绷。",
            details: "无酒精配方，搭配锌 PCA 控油成分，适合日常早晚清洁。",
            tags: ["油皮友好", "氨基酸", "无酒精"]
        ),
        Product(
            title: "轻量主动降噪蓝牙耳机",
            price: "¥189",
            reason: "预算 200 元内，续航和通勤降噪表现均衡。",
            details: "单次 7 小时续航，支持透明模式和低延迟游戏模式。",
            tags: ["200 元内", "降噪", "通勤"]
        ),
        Product(
            title: "云感缓震轻量跑鞋",
            price: "¥299",
            reason: "鞋面透气，单只重量低，适合 5-10 公里慢跑。",
            details: "中底回弹明显，后跟稳定片能降低轻度外翻风险。",
            tags: ["轻量", "缓震", "跑步"]
        ),
        Product(
            title: "清爽无酒精防晒乳 SPF50+",
            price: "¥159",
            reason: "肤感清爽，不含酒精，适合敏感肌日常通勤。",
            details: "成膜快，后续上妆不易搓泥，户外建议 2-3 小时补涂。",
            tags: ["无酒精", "SPF50+", "敏感肌"]
        )
    ]
}

struct ChatMessage: Identifiable {
    let id = UUID()
    let sender: Sender
    var text: String
    var state: MessageState = .ready
    var products: [Product] = []
    var canRetry: Bool = false
}

struct HistoryItem: Identifiable {
    let id = UUID()
    let title: String
    let subtitle: String
}
