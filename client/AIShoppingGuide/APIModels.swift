import Foundation

/// Empty-state categories and dynamic searches sourced from real inventory via /suggestions.
struct HomeSuggestions: Equatable {
    let categories: [String]
    let hotSearches: [String]
}

/// 首页的个性化内容：是否老用户，以及接着看的品类入口。
struct HomeFeed: Equatable {
    struct Entry: Identifiable, Equatable {
        var id: String { query }
        let title: String
        let query: String
        let imageURL: URL?
    }

    let isReturning: Bool
    let entries: [Entry]
}

struct APIErrorPayload: Codable, Hashable {
    let code: String
    let message: String
    let retryable: Bool
    let traceID: String?
}

struct Money: Codable, Hashable {
    let currency: String
    let amountMinor: Int
    let display: String
}

enum AttachmentKind: String, Codable, Hashable {
    case image
    case audio
}

struct AttachmentPayload: Identifiable, Codable, Hashable {
    let id: String
    let kind: AttachmentKind
    let mimeType: String
    let localURL: URL?
    let remoteURL: URL?
    let sizeBytes: Int?
    let metadata: [String: String]

    init(
        id: String = UUID().uuidString,
        kind: AttachmentKind,
        mimeType: String,
        localURL: URL? = nil,
        remoteURL: URL? = nil,
        sizeBytes: Int? = nil,
        metadata: [String: String] = [:]
    ) {
        self.id = id
        self.kind = kind
        self.mimeType = mimeType
        self.localURL = localURL
        self.remoteURL = remoteURL
        self.sizeBytes = sizeBytes
        self.metadata = metadata
    }
}

enum ProductAvailability: String, Codable, Hashable {
    case inStock
    case lowStock
    case outOfStock
    case unknown
}

struct ProductSpecOptionPayload: Identifiable, Codable, Hashable {
    let id: String
    let label: String
    let isAvailable: Bool
}

struct ProductSpecificationPayload: Identifiable, Codable, Hashable {
    let id: String
    let name: String
    let options: [ProductSpecOptionPayload]
}

struct SKUPayload: Identifiable, Codable, Hashable {
    let id: String
    let selectedOptions: [String: String]
    let price: Money
    let availability: ProductAvailability
    let stockCount: Int?
}

enum EvidenceSourceType: String, Codable, Hashable {
    case productDetail
    case userReview
    case adCopy
    case catalog
    case visualMatch
}

struct EvidencePayload: Identifiable, Codable, Hashable {
    let id: String
    let sourceType: EvidenceSourceType
    let title: String
    let snippet: String
    let score: Double?
    let updatedAt: Date?
}

struct ReviewPayload: Identifiable, Codable, Hashable {
    let id: String
    let nickname: String
    let rating: Int
    let content: String
    let polarity: String?
}

struct ProductPayload: Identifiable, Codable, Hashable {
    var id: String { productID }

    let productID: String
    let title: String
    let category: String
    let brand: String?
    let imageURL: URL?
    let detailURL: URL?
    let price: Money
    let originalPrice: Money?
    let availability: ProductAvailability
    let summary: String?
    let tags: [String]
    let specifications: [ProductSpecificationPayload]
    let skus: [SKUPayload]
    let reviews: [ReviewPayload]?
    let evidence: [EvidencePayload]?
    let updatedAt: Date?
    var faqs: [ProductFAQ]? = nil
    var priceType: String? = nil
    /// 亚马逊目录的来源信息，带平均评分和评分人数；中文目录没有。
    var catalogSource: CatalogSourcePayload? = nil
}

struct CatalogSourcePayload: Codable, Hashable {
    let averageRating: Double?
    let ratingNumber: Int?

    enum CodingKeys: String, CodingKey {
        case averageRating = "average_rating"
        case ratingNumber = "rating_number"
    }
}

struct ProductSearchRequest: Codable, Hashable {
    let query: String
    let filters: [String: String]
    let excludedTerms: [String]
    let limit: Int
}

struct ProductSearchResponse: Codable, Hashable {
    let requestID: String
    let products: [ProductPayload]
    let evidence: [EvidencePayload]?
}

/// 对比请求里的一件商品 + 可选的指定规格；不传规格由后端按预算 / 最便宜自动选。
struct ComparisonItemRequest: Codable, Hashable {
    let productID: String
    let skuID: String?
}

struct ProductComparisonRequest: Codable, Hashable {
    let productIDs: [String]
    let focus: String?
    /// 指定规格时用它（优先于 productIDs）。
    var items: [ComparisonItemRequest]? = nil
    var budgetMax: Double? = nil
}

/// 对比表里一个商品的规格（选中的 / 可切换的）。
struct ComparisonSKUPayload: Codable, Hashable, Identifiable {
    let skuID: String
    let label: String
    let price: Double
    let priceDisplay: String
    let reason: String?

    var id: String { skuID }

    enum CodingKeys: String, CodingKey {
        case skuID = "sku_id"
        case label
        case price
        case priceDisplay = "price_display"
        case reason
    }
}

/// Lightweight comparison-table product header aligned with /compare products[].
struct ComparisonProductPayload: Codable, Hashable, Identifiable {
    let productID: String
    let title: String
    let brand: String?
    let price: Double
    let imageURL: URL?
    /// 本次对比选中的规格（SKU 级对比）；老后端没有则为 nil。
    var sku: ComparisonSKUPayload? = nil
    /// 该商品全部可切换规格。
    var skus: [ComparisonSKUPayload]? = nil

    var id: String { productID }

    enum CodingKeys: String, CodingKey {
        case sku
        case skus
        case productID = "product_id"
        case title
        case brand
        case price
        case imageURL = "image_url"
    }
}

/// One comparison dimension with values ordered by product and an optional winner index.
struct ComparisonRowPayload: Codable, Hashable, Identifiable {
    let label: String
    let values: [String]
    let highlight: Int?

    var id: String { label }
}

struct ProductComparisonPayload: Codable, Hashable {
    let title: String
    let products: [ComparisonProductPayload]
    let rows: [ComparisonRowPayload]
    let recommendation: String
}

struct ClarificationOptionPayload: Identifiable, Codable, Hashable {
    let id: String
    let title: String
    let value: String
}

struct ClarificationPromptPayload: Codable, Hashable {
    let question: String
    let options: [ClarificationOptionPayload]
    let allowsFreeText: Bool
}

enum AgentRequestMode: String, Codable, Hashable {
    case recommend
    case compare
    case followUp
    case cartOperation
    case multimodalSearch
}

struct AgentRequestPayload: Codable, Hashable {
    let sessionID: String?
    let userID: String
    let clientMessageID: String
    let text: String
    let attachments: [AttachmentPayload]
    let mode: AgentRequestMode?
    let profile: UserProfilePayload?
    let cart: CartSnapshotPayload?
    let locale: String
    let timezone: String
    /// Optional base64 image data for visual search; nil for text-only requests.
    let imageBase64: String?

    init(
        sessionID: String? = nil,
        userID: String = UserIdentity.defaultUserID,
        clientMessageID: String = UUID().uuidString,
        text: String,
        attachments: [AttachmentPayload] = [],
        mode: AgentRequestMode? = nil,
        profile: UserProfilePayload? = nil,
        cart: CartSnapshotPayload? = nil,
        locale: String = Locale.current.identifier,
        timezone: String = TimeZone.current.identifier,
        imageBase64: String? = nil
    ) {
        self.sessionID = sessionID
        self.userID = userID
        self.clientMessageID = clientMessageID
        self.text = text
        self.attachments = attachments
        self.mode = mode
        self.profile = profile
        self.cart = cart
        self.locale = locale
        self.timezone = timezone
        self.imageBase64 = imageBase64
    }
}

enum AgentStatusPhase: String, Codable, Hashable {
    case understanding
    case retrieving
    case generating
    case executingTool
    case done
}

struct AgentStatusPayload: Codable, Hashable {
    let phase: AgentStatusPhase
    let message: String
}

enum AgentToolActionKind: String, Codable, Hashable {
    case addToCart
    case updateCartItem
    case removeCartItem
    case selectCartItems
    case createOrderPreview
    case confirmOrder
    case openProductDetail
}

struct AgentToolActionPayload: Codable, Hashable {
    let action: AgentToolActionKind
    let productID: String?
    let skuID: String?
    let cartItemID: String?
    let selectedOptions: [String: String]
    let quantity: Int?
    let selectedCartItemIDs: [String]
}

enum AgentStreamEventType: String, Codable, Hashable {
    case status
    case textDelta
    /// 结构化流式：开场白的一段增量文本（按到达顺序逐字渲染）。
    case openingDelta
    /// 结构化流式：某一款商品的专属解说已生成（此时弹出该商品的卡片）。
    case itemDescription
    /// 结构化流式：结尾总结。
    case summary
    /// 对比：一条差异解读。
    case comparePoint
    /// 对比：一条选购建议。
    case comparePick
    /// 结构化流式：一条追问 Prompt。
    case followup
    case products
    case comparison
    case clarification
    case specSelection
    case toolAction
    case cartSnapshot
    case memoryUpdate
    case error
    case done
}

struct MemoryUpdatePayload: Codable, Hashable {
    let message: String
    let field: String
    let value: StringValue
    let source: String
    let undoToken: String
    let preference: UserPreferencePayload?

    enum CodingKeys: String, CodingKey {
        case message
        case field
        case value
        case source
        case undoToken = "undo_token"
        case preference
    }
}

struct AgentStreamEventPayload: Identifiable, Codable, Hashable {
    let id: String
    let type: AgentStreamEventType
    let textDelta: String?
    let status: AgentStatusPayload?
    let products: [ProductPayload]
    let comparison: ProductComparisonPayload?
    let clarification: ClarificationPromptPayload?
    let specSelection: SpecSelection?
    let toolAction: AgentToolActionPayload?
    let cartSnapshot: CartSnapshotPayload?
    let memoryUpdate: MemoryUpdatePayload?
    let error: APIErrorPayload?
    let traceID: String?
    /// itemDescription 事件：解说对应的商品 ID；其余事件为 nil。
    let productID: String?
    /// itemDescription 事件：所属分区的小标题；没有分区时为 nil。
    let group: String?
    /// products 事件：按商品 ID 索引的预算匹配信息（后端 tool_result.products[].budget）。
    let budgets: [String: ProductBudgetHint]

    init(
        id: String = UUID().uuidString,
        type: AgentStreamEventType,
        textDelta: String? = nil,
        productID: String? = nil,
        group: String? = nil,
        budgets: [String: ProductBudgetHint] = [:],
        status: AgentStatusPayload? = nil,
        products: [ProductPayload] = [],
        comparison: ProductComparisonPayload? = nil,
        clarification: ClarificationPromptPayload? = nil,
        specSelection: SpecSelection? = nil,
        toolAction: AgentToolActionPayload? = nil,
        cartSnapshot: CartSnapshotPayload? = nil,
        memoryUpdate: MemoryUpdatePayload? = nil,
        error: APIErrorPayload? = nil,
        traceID: String? = nil
    ) {
        self.id = id
        self.type = type
        self.textDelta = textDelta
        self.productID = productID
        self.group = group
        self.budgets = budgets
        self.status = status
        self.products = products
        self.comparison = comparison
        self.clarification = clarification
        self.specSelection = specSelection
        self.toolAction = toolAction
        self.cartSnapshot = cartSnapshot
        self.memoryUpdate = memoryUpdate
        self.error = error
        self.traceID = traceID
    }
}

enum UserIdentity {
    static let defaultUserID = "local-user"
}

enum StringValue: Codable, Hashable {
    case string(String)
    case number(Double)
    case object([String: Double])
    case array([String])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode([String].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: Double].self) {
            self = .object(value)
        } else {
            self = .null
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .string(value):
            try container.encode(value)
        case let .number(value):
            try container.encode(value)
        case let .object(value):
            try container.encode(value)
        case let .array(value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    var displayText: String {
        switch self {
        case let .string(value): return value
        case let .number(value): return "\(Int(value))"
        case let .array(value): return value.joined(separator: ", ")
        case let .object(value):
            if let min = value["budget_min"], let max = value["budget_max"] {
                return "¥\(Int(min))-¥\(Int(max))"
            }
            return ""
        case .null: return ""
        }
    }
}

struct UserPreferencePayload: Codable, Hashable {
    let userID: String
    var personalizationEnabled: Bool
    var budgetMin: Double?
    var budgetMax: Double?
    var priceTier: String?
    var favoriteCategories: [String]
    var brandInclude: [String]
    var brandExclude: [String]
    var preferenceKeywords: [String]
    var styleTags: [String]
    var categorySpecific: [String: [String: StringValue]]
    var preferenceNote: String?
    var notes: String?

    enum CodingKeys: String, CodingKey {
        case userID = "user_id"
        case personalizationEnabled = "personalization_enabled"
        case budgetMin = "budget_min"
        case budgetMax = "budget_max"
        case priceTier = "price_tier"
        case favoriteCategories = "favorite_categories"
        case brandInclude = "brand_include"
        case brandExclude = "brand_exclude"
        case preferenceKeywords = "preference_keywords"
        case styleTags = "style_tags"
        case categorySpecific = "category_specific"
        case preferenceNote = "preference_note"
        case notes
    }

    init(
        userID: String,
        personalizationEnabled: Bool,
        budgetMin: Double?,
        budgetMax: Double?,
        priceTier: String?,
        favoriteCategories: [String],
        brandInclude: [String],
        brandExclude: [String],
        preferenceKeywords: [String],
        styleTags: [String],
        categorySpecific: [String: [String: StringValue]],
        preferenceNote: String?,
        notes: String?
    ) {
        self.userID = userID
        self.personalizationEnabled = personalizationEnabled
        self.budgetMin = budgetMin
        self.budgetMax = budgetMax
        self.priceTier = priceTier
        self.favoriteCategories = favoriteCategories
        self.brandInclude = brandInclude
        self.brandExclude = brandExclude
        self.preferenceKeywords = preferenceKeywords
        self.styleTags = styleTags
        self.categorySpecific = categorySpecific
        self.preferenceNote = preferenceNote
        self.notes = notes
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let styleTags = try container.decodeIfPresent([String].self, forKey: .styleTags) ?? []
        let keywords = try container.decodeIfPresent([String].self, forKey: .preferenceKeywords) ?? styleTags
        self.init(
            userID: try container.decode(String.self, forKey: .userID),
            personalizationEnabled: try container.decodeIfPresent(Bool.self, forKey: .personalizationEnabled) ?? true,
            budgetMin: try container.decodeIfPresent(Double.self, forKey: .budgetMin),
            budgetMax: try container.decodeIfPresent(Double.self, forKey: .budgetMax),
            priceTier: try container.decodeIfPresent(String.self, forKey: .priceTier),
            favoriteCategories: try container.decodeIfPresent([String].self, forKey: .favoriteCategories) ?? [],
            brandInclude: try container.decodeIfPresent([String].self, forKey: .brandInclude) ?? [],
            brandExclude: try container.decodeIfPresent([String].self, forKey: .brandExclude) ?? [],
            preferenceKeywords: keywords,
            styleTags: styleTags,
            categorySpecific: try container.decodeIfPresent([String: [String: StringValue]].self, forKey: .categorySpecific) ?? [:],
            preferenceNote: try container.decodeIfPresent(String.self, forKey: .preferenceNote),
            notes: try container.decodeIfPresent(String.self, forKey: .notes)
        )
    }

    static func empty(userID: String = UserIdentity.defaultUserID) -> UserPreferencePayload {
        UserPreferencePayload(
            userID: userID,
            personalizationEnabled: true,
            budgetMin: nil,
            budgetMax: nil,
            priceTier: "balanced",
            favoriteCategories: [],
            brandInclude: [],
            brandExclude: [],
            preferenceKeywords: [],
            styleTags: [],
            categorySpecific: [:],
            preferenceNote: nil,
            notes: nil
        )
    }
}

struct PlacedOrderPayload: Codable, Hashable {
    let orderID: String
    let total: Double
    let itemCount: Int

    enum CodingKeys: String, CodingKey {
        case total
        case orderID = "order_id"
        case itemCount = "item_count"
    }
}

struct PlacedOrderEnvelope: Codable {
    let order: PlacedOrderPayload
}

/// 「我的」页的历史订单（GET /orders/{user_id}）
struct OrderPayload: Codable, Identifiable, Hashable {
    let orderID: String
    let status: String
    let total: Double
    let createdAt: String
    let lines: [OrderLinePayload]

    var id: String { orderID }
    var itemCount: Int { lines.reduce(0) { $0 + $1.quantity } }

    enum CodingKeys: String, CodingKey {
        case status, total, lines
        case orderID = "order_id"
        case createdAt = "created_at"
    }
}

struct OrderLinePayload: Codable, Hashable {
    let productID: String
    let title: String
    let quantity: Int
    let unitPrice: Double

    enum CodingKeys: String, CodingKey {
        case title, quantity
        case productID = "product_id"
        case unitPrice = "unit_price"
    }
}

/// 详情页「About this item」的一条要点（GET /products/{id}/highlights）
struct ProductHighlight: Codable, Identifiable, Hashable {
    let emoji: String
    let title: String
    let detail: String

    var id: String { title }
}

struct ProductHighlightEnvelope: Codable {
    let highlights: [ProductHighlight]
}

struct OrderListEnvelope: Codable {
    let orders: [OrderPayload]
}


// MARK: - 记忆页

struct MemoryItemPayload: Identifiable, Codable, Hashable {
    let id: String
    let kind: String
    let group: String
    let label: String
    let scope: String?
    let source: String
    let sourceLabel: String
    let evidence: String?
    let effect: String
    let needsCare: Bool
    let useCount: Int

    enum CodingKeys: String, CodingKey {
        case id, kind, group, label, scope, source, evidence, effect
        case sourceLabel = "source_label"
        case needsCare = "needs_care"
        case useCount = "use_count"
    }
}

struct MemoryGroupPayload: Identifiable, Codable, Hashable {
    let name: String
    let items: [MemoryItemPayload]
    var id: String { name }
}

struct MemoryActivityPayload: Identifiable, Codable, Hashable {
    let id: String
    let type: String
    let text: String
    let at: String
}

struct MemoryViewPayload: Codable, Hashable {
    let userID: String
    let personalizationEnabled: Bool
    let summary: String
    let groups: [MemoryGroupPayload]
    let suggestions: [MemoryItemPayload]
    let activity: [MemoryActivityPayload]
    let count: Int

    enum CodingKeys: String, CodingKey {
        case summary, groups, suggestions, activity, count
        case userID = "user_id"
        case personalizationEnabled = "personalization_enabled"
    }
}

struct MemoryActionResult: Codable, Hashable {
    let message: String
    let undoToken: String?
    let view: MemoryViewPayload

    private struct Update: Codable, Hashable {
        let undoToken: String?
        enum CodingKeys: String, CodingKey { case undoToken = "undo_token" }
    }

    enum CodingKeys: String, CodingKey {
        case message, view, updates
        case undoToken = "undo_token"
    }

    init(message: String, undoToken: String?, view: MemoryViewPayload) {
        self.message = message
        self.undoToken = undoToken
        self.view = view
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        message = try container.decode(String.self, forKey: .message)
        view = try container.decode(MemoryViewPayload.self, forKey: .view)
        let direct = try container.decodeIfPresent(String.self, forKey: .undoToken)
        let updates = try container.decodeIfPresent([Update].self, forKey: .updates)
        undoToken = direct ?? updates?.compactMap(\.undoToken).first
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(message, forKey: .message)
        try container.encodeIfPresent(undoToken, forKey: .undoToken)
        try container.encode(view, forKey: .view)
    }
}

struct PreferenceUndoRequest: Codable, Hashable {
    let undoToken: String

    enum CodingKeys: String, CodingKey {
        case undoToken = "undo_token"
    }
}

struct CartItemPayload: Identifiable, Codable, Hashable {
    let id: String
    let productID: String
    let skuID: String?
    let product: ProductPayload
    let selectedOptions: [String: String]
    let quantity: Int
    let isSelected: Bool
    let lineTotal: Money
}

struct CartPriceSummaryPayload: Codable, Hashable {
    let subtotal: Money
    let discount: Money?
    let payable: Money
}

struct CartSnapshotPayload: Codable, Hashable {
    let cartID: String
    let items: [CartItemPayload]
    let selectedItemIDs: [String]
    let priceSummary: CartPriceSummaryPayload
    let updatedAt: Date
}

enum CartMutationKind: String, Codable, Hashable {
    case add
    case updateQuantity
    case updateSpecification
    case remove
    case select
    case deselect
    case selectAll
    case clearSelection
}

struct CartMutationRequest: Codable, Hashable {
    let action: CartMutationKind
    let productID: String?
    let skuID: String?
    let cartItemID: String?
    let selectedOptions: [String: String]
    let quantity: Int?
    let selectedCartItemIDs: [String]

    init(
        action: CartMutationKind,
        productID: String? = nil,
        skuID: String? = nil,
        cartItemID: String? = nil,
        selectedOptions: [String: String] = [:],
        quantity: Int? = nil,
        selectedCartItemIDs: [String] = []
    ) {
        self.action = action
        self.productID = productID
        self.skuID = skuID
        self.cartItemID = cartItemID
        self.selectedOptions = selectedOptions
        self.quantity = quantity
        self.selectedCartItemIDs = selectedCartItemIDs
    }
}

struct AddressPayload: Identifiable, Codable, Hashable {
    let id: String
    let receiverName: String
    let phoneMasked: String
    let fullAddress: String
    let isDefault: Bool
}

struct OrderLineItemPayload: Identifiable, Codable, Hashable {
    let id: String
    let product: ProductPayload
    let selectedOptions: [String: String]
    let quantity: Int
    let lineTotal: Money
}

struct OrderPreviewPayload: Codable, Hashable {
    let orderID: String
    let items: [OrderLineItemPayload]
    let address: AddressPayload?
    let priceSummary: CartPriceSummaryPayload
    let warnings: [String]
}

struct OrderConfirmRequest: Codable, Hashable {
    let orderID: String
    let addressID: String
}

struct OrderConfirmationPayload: Codable, Hashable {
    let orderID: String
    let status: String
    let createdAt: Date
}

struct UserProfilePayload: Codable, Hashable {
    let userID: String
    let budgetMin: Money?
    let budgetMax: Money?
    let preferredCategories: [String]
    let excludedIngredients: [String]
    let excludedBrands: [String]
    let attributes: [String: String]
    let defaultAddressID: String?
}

struct UserProfileUpdateRequest: Codable, Hashable {
    let budgetMin: Money?
    let budgetMax: Money?
    let preferredCategories: [String]?
    let excludedIngredients: [String]?
    let excludedBrands: [String]?
    let attributes: [String: String]?
    let defaultAddressID: String?
}

enum UploadIntent: String, Codable, Hashable {
    case visualSearch
    case chatImage
    case speechInput
}

struct UploadTicketPayload: Codable, Hashable {
    let uploadID: String
    let uploadURL: URL
    let expiresAt: Date
    let headers: [String: String]
}

struct AnalyticsEventPayload: Codable, Hashable {
    let name: String
    let properties: [String: String]
    let createdAt: Date

    init(name: String, properties: [String: String] = [:], createdAt: Date = Date()) {
        self.name = name
        self.properties = properties
        self.createdAt = createdAt
    }
}
