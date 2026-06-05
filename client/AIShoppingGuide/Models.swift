import Foundation

enum Sender: String, Codable {
    case user
    case ai
}

enum MessageState: String, Codable {
    case understanding = "理解中"
    case retrieving = "检索中"
    case generating = "生成中"
    case ready
    case failed
}

struct Product: Identifiable, Hashable, Codable {
    let id: String
    let title: String
    let price: String
    let reason: String
    let details: String
    let tags: [String]
    let specifications: [ProductSpecification]
    let imageURL: URL?
    let skus: [ProductSKU]

    init(
        id: String = UUID().uuidString,
        title: String,
        price: String,
        reason: String,
        details: String,
        tags: [String],
        specifications: [ProductSpecification],
        imageURL: URL? = nil,
        skus: [ProductSKU] = []
    ) {
        self.id = id
        self.title = title
        self.price = price
        self.reason = reason
        self.details = details
        self.tags = tags
        self.specifications = specifications
        self.imageURL = imageURL
        self.skus = skus
    }

    var defaultSpecificationSelection: [String: String] {
        if let lowestSKU {
            return lowestSKU.selectedOptions
        }
        return specifications.reduce(into: [:]) { result, specification in
            if let firstOption = specification.options.first {
                result[specification.name] = firstOption
            }
        }
    }

    var lowestSKU: ProductSKU? {
        skus.min { $0.price < $1.price }
    }

    func matchingSKU(for selectedOptions: [String: String]) -> ProductSKU? {
        guard !skus.isEmpty else { return nil }
        return skus.first { $0.selectedOptions == selectedOptions }
    }

    func displaySKU(for selectedOptions: [String: String]) -> ProductSKU? {
        matchingSKU(for: selectedOptions) ?? lowestSKU
    }

    func priceValue(for selectedOptions: [String: String]) -> Double {
        if let sku = displaySKU(for: selectedOptions) {
            return sku.price
        }
        return Product.numericPrice(from: price)
    }

    func priceDisplay(for selectedOptions: [String: String]) -> String {
        displaySKU(for: selectedOptions)?.priceDisplay ?? price
    }

    static func numericPrice(from display: String) -> Double {
        let cleaned = display
            .replacingOccurrences(of: "¥", with: "")
            .replacingOccurrences(of: ",", with: "")
            .replacingOccurrences(of: "起", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return Double(cleaned) ?? 0
    }

    static let samples: [Product] = [
        Product(
            title: "温和氨基酸控油洗面奶",
            price: "¥129",
            reason: "适合油皮与混合肌，清洁力足够但洗后不紧绷。",
            details: "无酒精配方，搭配锌 PCA 控油成分，适合日常早晚清洁。",
            tags: ["油皮友好", "氨基酸", "无酒精"],
            specifications: [
                ProductSpecification(name: "容量", options: ["120g", "180g"]),
                ProductSpecification(name: "套装", options: ["单支", "2 支装"])
            ]
        ),
        Product(
            title: "轻量主动降噪蓝牙耳机",
            price: "¥189",
            reason: "预算 200 元内，续航和通勤降噪表现均衡。",
            details: "单次 7 小时续航，支持透明模式和低延迟游戏模式。",
            tags: ["200 元内", "降噪", "通勤"],
            specifications: [
                ProductSpecification(name: "颜色", options: ["曜石黑", "云雾白", "海盐蓝"]),
                ProductSpecification(name: "版本", options: ["标准版", "长续航版"])
            ]
        ),
        Product(
            title: "云感缓震轻量跑鞋",
            price: "¥299",
            reason: "鞋面透气，单只重量低，适合 5-10 公里慢跑。",
            details: "中底回弹明显，后跟稳定片能降低轻度外翻风险。",
            tags: ["轻量", "缓震", "跑步"],
            specifications: [
                ProductSpecification(name: "尺码", options: ["39", "40", "41", "42", "43"]),
                ProductSpecification(name: "颜色", options: ["雾灰", "曜石黑", "荧光绿"])
            ]
        ),
        Product(
            title: "清爽无酒精防晒乳 SPF50+",
            price: "¥159",
            reason: "肤感清爽，不含酒精，适合敏感肌日常通勤。",
            details: "成膜快，后续上妆不易搓泥，户外建议 2-3 小时补涂。",
            tags: ["无酒精", "SPF50+", "敏感肌"],
            specifications: [
                ProductSpecification(name: "容量", options: ["50ml", "90ml"]),
                ProductSpecification(name: "肤感", options: ["清爽型", "保湿型"])
            ]
        )
    ]
}

struct ProductSpecification: Identifiable, Hashable, Codable {
    var id = UUID()
    let name: String
    let options: [String]
}

struct ProductSKU: Identifiable, Hashable, Codable {
    let id: String
    let selectedOptions: [String: String]
    let price: Double
    let priceDisplay: String
}

struct CartItem: Identifiable, Hashable {
    let product: Product
    let selectedOptions: [String: String]
    var quantity: Int

    init(product: Product, selectedOptions: [String: String] = [:], quantity: Int = 1) {
        self.product = product
        self.selectedOptions = selectedOptions
        self.quantity = quantity
    }

    var id: String {
        [product.id, specificationKey]
            .filter { !$0.isEmpty }
            .joined(separator: "|")
    }

    var specificationSummary: String {
        sortedOptions
            .map { "\($0.key)：\($0.value)" }
            .joined(separator: "  ")
    }

    private var specificationKey: String {
        sortedOptions
            .map { "\($0.key)=\($0.value)" }
            .joined(separator: "|")
    }

    private var sortedOptions: [(key: String, value: String)] {
        selectedOptions.sorted { $0.key < $1.key }
    }
}

struct ChatMessage: Identifiable, Codable {
    var id = UUID()
    let sender: Sender
    var text: String
    var state: MessageState = .ready
    var products: [Product] = []
    var canRetry: Bool = false
}

/// 一段完整对话：对应后端一个 session_id，整段 transcript 本地持久化。
struct Conversation: Identifiable, Codable {
    let id: UUID
    /// 后端会话 id：重开历史后继续追问仍接到同一上下文。
    let sessionID: String
    var title: String
    var createdAt: Date
    var updatedAt: Date
    var messages: [ChatMessage]

    init(
        id: UUID = UUID(),
        sessionID: String = UUID().uuidString,
        title: String = "新对话",
        createdAt: Date = Date(),
        updatedAt: Date = Date(),
        messages: [ChatMessage] = []
    ) {
        self.id = id
        self.sessionID = sessionID
        self.title = title
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.messages = messages
    }

    /// 是否已有真实用户消息（用于判断空白对话不入库）。
    var hasUserMessage: Bool {
        messages.contains { $0.sender == .user }
    }

    private var productCount: Int {
        messages.reduce(0) { $0 + $1.products.count }
    }

    /// 历史列表副标题：相对时间 +（可选）商品数。
    var subtitle: String {
        let time = Conversation.relativeLabel(for: updatedAt)
        return productCount > 0 ? "\(time) · \(productCount) 个商品" : time
    }

    private static func relativeLabel(for date: Date) -> String {
        let calendar = Calendar.current
        let timeFormatter = DateFormatter()
        timeFormatter.locale = Locale(identifier: "zh_CN")
        timeFormatter.dateFormat = "HH:mm"
        if calendar.isDateInToday(date) {
            return "今天 \(timeFormatter.string(from: date))"
        }
        if calendar.isDateInYesterday(date) {
            return "昨天 \(timeFormatter.string(from: date))"
        }
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "zh_CN")
        dayFormatter.dateFormat = "M月d日 HH:mm"
        return dayFormatter.string(from: date)
    }
}
