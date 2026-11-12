import Foundation

enum Sender: String, Codable {
    case user
    case ai
}

enum MessageState: String, Codable {
    case understanding = "Understanding…"
    case retrieving = "Searching…"
    case generating = "Writing…"
    case ready
    case failed

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let value = try container.decode(String.self)
        let old: [String: Self] = ["理解中": .understanding, "检索中": .retrieving, "生成中": .generating]
        guard let state = Self(rawValue: value) ?? old[value] else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unknown chat state")
        }
        self = state
    }
}

/// 导购给出预算时，后端随 tool_result 下发的「预算匹配」信息：预算内有几个规格、最便宜的是哪个。
struct ProductBudgetHint: Hashable, Codable {
    let maxPrice: Double?
    let minPrice: Double?
    let skusWithinBudget: Int
    let skuCount: Int
    let cheapestSKUID: String?
    let cheapestPrice: Double?

    /// 卡片上的一句提示：「预算内 2 个规格」/「全部规格都在预算内」/「仅基础款在预算内」。
    var caption: String? {
        guard maxPrice != nil || minPrice != nil else { return nil }
        if skusWithinBudget <= 0 { return "No options within budget" }
        if skuCount > 1 && skusWithinBudget == skuCount { return "All options within budget" }
        if skuCount > 1 { return "\(skusWithinBudget) options within budget" }
        return nil
    }
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
    /// 本轮推荐的预算匹配信息；非预算类检索为 nil。
    var budgetHint: ProductBudgetHint? = nil
    /// 商品问答，详情页"问问导购"用。旧对话里存的商品没有这个字段。
    var faqs: [ProductFAQ]? = nil
    var referenceOnly: Bool? = nil

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
        if let sku = budgetDefaultSKU {
            return sku.selectedOptions
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

    var highestSKU: ProductSKU? {
        skus.max { $0.price < $1.price }
    }

    /// 多规格商品给完整区间「¥450 – ¥900」，单规格给单价；没有 SKU 数据时退回后端 price 文案。
    var priceRangeDisplay: String {
        guard let low = lowestSKU, let high = highestSKU, high.price > low.price else {
            return lowestSKU?.priceDisplay ?? price
        }
        return "\(low.priceDisplay) – \(high.priceDisplay)"
    }

    /// 预算内最便宜的规格（有预算提示时），否则最低价规格。
    var budgetDefaultSKU: ProductSKU? {
        if let id = budgetHint?.cheapestSKUID, let sku = skus.first(where: { $0.id == id }) {
            return sku
        }
        return lowestSKU
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
            .replacingOccurrences(of: "$", with: "")
            .replacingOccurrences(of: "¥", with: "")
            .replacingOccurrences(of: " (reference)", with: "")
            .replacingOccurrences(of: ",", with: "")
            .replacingOccurrences(of: "起", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return Double(cleaned) ?? 0
    }

    static let samples: [Product] = [
        Product(
            title: "Gentle foaming face wash",
            price: "$12.99",
            reason: "A gentle cleanser for oily or combination skin.",
            details: "Alcohol-free formula with zinc PCA for daily use.",
            tags: ["Oily skin", "Amino acids", "Alcohol-free"],
            specifications: [
                ProductSpecification(name: "Volume", options: ["120g", "180g"]),
                ProductSpecification(name: "Pack size", options: ["Single tube", "Two-pack"])
            ]
        ),
        Product(
            title: "Lightweight noise-canceling earbuds",
            price: "$49.99",
            reason: "Balances battery life and noise cancellation for commuting.",
            details: "Up to seven hours per charge, with transparency and low-latency modes.",
            tags: ["Under $50", "Noise canceling", "Commuting"],
            specifications: [
                ProductSpecification(name: "Color", options: ["Black", "White", "Blue"]),
                ProductSpecification(name: "Edition", options: ["Standard", "Extended battery"])
            ]
        ),
        Product(
            title: "Lightweight cushioned running shoes",
            price: "$89.99",
            reason: "A breathable upper for shorter runs.",
            details: "Responsive cushioning and a supportive heel.",
            tags: ["Lightweight", "Cushioning", "Running"],
            specifications: [
                ProductSpecification(name: "Size", options: ["39", "40", "41", "42", "43"]),
                ProductSpecification(name: "Color", options: ["Gray", "Black", "Lime"])
            ]
        ),
        Product(
            title: "Alcohol-free SPF 50+ sunscreen",
            price: "$15.99",
            reason: "A light, alcohol-free sunscreen for daily use.",
            details: "Applies quickly and layers under makeup.",
            tags: ["Alcohol-free", "SPF50+", "Sensitive skin"],
            specifications: [
                ProductSpecification(name: "Volume", options: ["50ml", "90ml"]),
                ProductSpecification(name: "Finish", options: ["Light", "Moisturizing"])
            ]
        )
    ]
}

struct ProductFAQ: Identifiable, Hashable, Codable {
    var id: String { question }
    let question: String
    let answer: String
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
    /// Interactive variant-selection card returned by the AI when adding a multi-variant product; otherwise nil.
    var specSelection: SpecSelection? = nil
    /// Structured product comparison returned by the AI; otherwise nil.
    var comparison: ProductComparisonPayload? = nil
    /// Local image attached to this message for visual search and thumbnail rendering; otherwise nil.
    var localImageData: Data? = nil
    /// Structured content returned by the AI in JSON format.
    var structuredContent: StructuredContent? = nil
    /// 商品卡片已先展示时，最终 composer 完成后追加的完成态说明。
    var completionSummary: String? = nil
    /// 推荐卡片已展示、但追问 Prompt 仍在生成时，底部展示独立加载态。
    var isGeneratingFollowups: Bool = false
    /// 只影响追问 Prompt 的失败信息；商品卡片与正文保持可用。
    var followupError: String? = nil
    /// 流式进行中显示在消息底部的「生成中」文案（正在介绍商品 / 生成追问中）；nil 不显示。
    var streamingStatusText: String? = nil
    /// 用户中途点了停止，保留的是半截回答。必须是可选类型，否则旧对话记录解码会失败。
    var wasStopped: Bool? = nil
    /// 是因为用户改了需求才停的，新结果在后面的消息里。
    var wasAmended: Bool? = nil
    /// 回答开始前展示的思考步骤，最后一条是正在进行的。回答一开始就不再显示。
    var thinkingSteps: [String]? = nil
    /// 用户对这条回答的评价：up / down；没评价时为 nil。
    var feedback: String? = nil
}

struct StructuredContent: Codable {
    let opening: String
    let items: [StructuredItem]
    /// Follow-up prompt that can populate the input field without being sent automatically.
    let followup: [String]
    /// 所有商品之后的结尾总结：谁该选哪款，再问用户一句。旧数据和单款推荐没有。
    let summary: String?
    /// 对比时：这些差异在使用中意味着什么。
    let points: [String]?
    /// 对比时：什么情况选哪款。
    let picks: [String]?

    enum CodingKeys: String, CodingKey {
        case opening, items, followup, questions, summary, points, picks
    }

    init(
        opening: String,
        items: [StructuredItem],
        followup: [String] = [],
        summary: String? = nil,
        points: [String]? = nil,
        picks: [String]? = nil
    ) {
        self.opening = opening
        self.items = items
        self.followup = followup
        self.summary = summary
        self.points = points
        self.picks = picks
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
        summary = try container.decodeIfPresent(String.self, forKey: .summary)
        points = try container.decodeIfPresent([String].self, forKey: .points)
        picks = try container.decodeIfPresent([String].self, forKey: .picks)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(opening, forKey: .opening)
        try container.encode(items, forKey: .items)
        try container.encode(followup, forKey: .followup)
        try container.encodeIfPresent(summary, forKey: .summary)
        try container.encodeIfPresent(points, forKey: .points)
        try container.encodeIfPresent(picks, forKey: .picks)
    }
}

struct StructuredItem: Codable {
    let productId: String
    let description: String
    /// 所属分区的小标题；没有分区时为 nil。
    var group: String? = nil
    
    enum CodingKeys: String, CodingKey {
        case productId
        case description
        case group
    }
}

extension StructuredContent {
    /// Parses structured content from the narrative JSON returned by the composer.
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

/// Shopping recommendation copy with redundant details such as prices removed.
enum RecommendationCopy {
    private static let pricePatterns = [
        #"\$\s*[\d,]+(?:\.\d+)?(?:\s*\(reference\))?"#,
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
    /// 分享和反馈用的纯文本：开场白、商品、解读和总结，不含内部 JSON。
    var shareText: String {
        guard let content = structuredContent ?? StructuredContent.parse(from: text) else { return text }
        var lines = [content.opening]
        lines += products.map { "· \($0.title)（\($0.priceRangeDisplay)）" }
        lines += content.points ?? []
        lines += content.picks ?? []
        if let summary = content.summary { lines.append(summary) }
        // 总结和建议里的 **商品名** 是给界面高亮用的，分享出去的是纯文本
        return lines.filter { !$0.isEmpty }.joined(separator: "\n").replacingOccurrences(of: "**", with: "")
    }

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

/// One selectable dimension of a multi-variant product, such as color and its values.
struct SpecDimension: Identifiable, Hashable, Codable {
    var id = UUID()
    let name: String
    let values: [String]
}

/// Data required for one variant-selection interaction: the product and values for each dimension.
/// The selected values are converted to natural language and sent to the backend for an exact cart addition.
struct SpecSelection: Identifiable, Hashable, Codable {
    var id = UUID()
    let productID: String
    let title: String
    let dimensions: [SpecDimension]
}

/// A complete conversation associated with one backend session ID and persisted locally.
struct Conversation: Identifiable, Codable {
    let id: UUID
    /// Backend session ID used to preserve context when reopening a conversation.
    let sessionID: String
    var title: String
    var createdAt: Date
    var updatedAt: Date
    var messages: [ChatMessage]

    init(
        id: UUID = UUID(),
        sessionID: String = UUID().uuidString,
        title: String = "New chat",
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

    /// Whether the conversation contains a real user message, used to avoid saving empty conversations.
    var hasUserMessage: Bool {
        messages.contains { $0.sender == .user }
    }

    private var productCount: Int {
        messages.reduce(0) { $0 + $1.products.count }
    }

    /// History-list subtitle containing relative time and, optionally, the product count.
    var subtitle: String {
        let time = Conversation.relativeLabel(for: updatedAt)
        return productCount > 0 ? "\(time) · \(productCount) products" : time
    }

    private static func relativeLabel(for date: Date) -> String {
        let calendar = Calendar.current
        let timeFormatter = DateFormatter()
        timeFormatter.locale = Locale(identifier: "en_US")
        timeFormatter.dateFormat = "HH:mm"
        if calendar.isDateInToday(date) {
            return "Today \(timeFormatter.string(from: date))"
        }
        if calendar.isDateInYesterday(date) {
            return "Yesterday \(timeFormatter.string(from: date))"
        }
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US")
        dayFormatter.dateFormat = "MMM d, h:mm a"
        return dayFormatter.string(from: date)
    }
}
