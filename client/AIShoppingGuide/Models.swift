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
    let reviews: [ProductReview]

    init(
        id: String = UUID().uuidString,
        title: String,
        price: String,
        reason: String,
        details: String,
        tags: [String],
        specifications: [ProductSpecification],
        imageURL: URL? = nil,
        skus: [ProductSKU] = [],
        reviews: [ProductReview] = []
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
        self.reviews = reviews
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

struct ProductReview: Identifiable, Hashable, Codable {
    let id: String
    let nickname: String
    let rating: Int
    let content: String
    let polarity: String?
}

struct CartItem: Identifiable, Hashable {
    let product: Product
    let selectedOptions: [String: String]
    var quantity: Int
    let backendCartItemID: String?
    let skuID: String?

    init(
        product: Product,
        selectedOptions: [String: String] = [:],
        quantity: Int = 1,
        backendCartItemID: String? = nil,
        skuID: String? = nil
    ) {
        self.product = product
        self.selectedOptions = selectedOptions
        self.quantity = quantity
        self.backendCartItemID = backendCartItemID
        self.skuID = skuID
    }

    init(payload: CartItemPayload) {
        self.init(
            product: Product(payload: payload.product),
            selectedOptions: payload.selectedOptions,
            quantity: payload.quantity,
            backendCartItemID: payload.id,
            skuID: payload.skuID
        )
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
    /// 多规格商品加购时，AI 给出的可交互规格选择卡片；无则为 nil。
    var specSelection: SpecSelection? = nil
    /// 商品对比结果：AI 返回的结构化对比表；无则为 nil。
    var comparison: ProductComparisonPayload? = nil
    /// 拍照找货：用户这条消息附带的本地图片（缩略图渲染用）；无则为 nil。
    var localImageData: Data? = nil
    /// AI 返回的结构化内容（JSON 格式）
    var structuredContent: StructuredContent? = nil
    /// 商品卡片已先展示时，最终 composer 完成后追加的完成态说明。
    var completionSummary: String? = nil
    /// 推荐卡片已展示、但追问 Prompt 仍在生成时，底部展示独立加载态。
    var isGeneratingFollowups: Bool = false
    /// 只影响追问 Prompt 的失败信息；商品卡片与正文保持可用。
    var followupError: String? = nil
}

struct StructuredContent: Codable {
    let opening: String
    let items: [StructuredItem]
    /// 可直接填入输入框的追问 Prompt，点击后不自动发送。
    let followup: [String]

    enum CodingKeys: String, CodingKey {
        case opening, items, followup, questions
    }

    init(opening: String, items: [StructuredItem], followup: [String] = []) {
        self.opening = opening
        self.items = items
        self.followup = followup
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        opening = try container.decode(String.self, forKey: .opening)
        items = try container.decode([StructuredItem].self, forKey: .items)
        if let followup = try container.decodeIfPresent([String].self, forKey: .followup) {
            self.followup = followup
        } else {
            self.followup = try container.decodeIfPresent([String].self, forKey: .questions) ?? []
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(opening, forKey: .opening)
        try container.encode(items, forKey: .items)
        try container.encode(followup, forKey: .followup)
    }
}

struct StructuredItem: Codable {
    let productId: String
    let description: String
    
    enum CodingKeys: String, CodingKey {
        case productId
        case description
    }
}

extension StructuredContent {
    /// 从 composer 返回的 narrative JSON 解析结构化内容。
    static func parse(from text: String) -> StructuredContent? {
        guard let json = firstJSONObject(in: text),
              let data = json.data(using: .utf8)
        else { return nil }
        return try? JSONDecoder().decode(StructuredContent.self, from: data)
    }

    private static func firstJSONObject(in text: String) -> String? {
        var startIndex: String.Index?
        var depth = 0
        var isInString = false
        var isEscaped = false

        for index in text.indices {
            let character = text[index]

            if isInString {
                if isEscaped {
                    isEscaped = false
                } else if character == "\\" {
                    isEscaped = true
                } else if character == "\"" {
                    isInString = false
                }
                continue
            }

            if character == "\"" {
                isInString = true
            } else if character == "{" {
                if depth == 0 {
                    startIndex = index
                }
                depth += 1
            } else if character == "}", depth > 0 {
                depth -= 1
                if depth == 0, let startIndex {
                    return String(text[startIndex...index])
                }
            }
        }
        return nil
    }
}

/// 导购推荐理由文案：去掉价格等冗余信息。
enum RecommendationCopy {
    private static let pricePatterns = [
        #"售价\s*[¥￥]?\s*[\d,]+(?:\.\d+)?起?"#,
        #"[（(]\s*[\d,]+(?:\.\d+)?\s*元\s*[）)]"#,
        #"[¥￥]\s*[\d,]+(?:\.\d+)?起?"#,
    ]

    static func sanitized(_ raw: String) -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return "" }
        for pattern in pricePatterns {
            text = text.replacingOccurrences(
                of: pattern,
                with: "",
                options: .regularExpression
            )
        }
        text = text
            .replacingOccurrences(of: "：，", with: "：")
            .replacingOccurrences(of: "，，", with: "，")
            .replacingOccurrences(of: "  ", with: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: "，,、：: "))
        return text
    }

}

extension ChatMessage {
    /// 从本条 AI 消息的结构化 JSON 里取某商品的专属解说（与列表页逻辑一致）。
    func recommendationDescription(for productID: String) -> String? {
        guard sender == .ai, state == .ready else { return nil }
        guard let content = structuredContent ?? StructuredContent.parse(from: text) else { return nil }

        let item = content.items.first { $0.productId == productID }
            ?? products.firstIndex(where: { $0.id == productID })
                .flatMap { idx in idx < content.items.count ? content.items[idx] : nil }

        guard let description = item?.description else { return nil }
        let cleaned = RecommendationCopy.sanitized(description)
        return cleaned.isEmpty ? nil : cleaned
    }
}

struct ProductDetailContext: Identifiable, Hashable {
    let product: Product

    var id: String { product.id }
}

/// 多规格商品的一个可选维度（如「颜色」对应一组取值）。
struct SpecDimension: Identifiable, Hashable, Codable {
    var id = UUID()
    let name: String
    let values: [String]
}

/// 一次「请选择规格」交互所需的全部数据：商品 + 各维度可选值。
/// 用户在卡片上逐维度点选后，组合成自然语言发回后端完成精确加购。
struct SpecSelection: Identifiable, Hashable, Codable {
    var id = UUID()
    let productID: String
    let title: String
    let dimensions: [SpecDimension]
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
