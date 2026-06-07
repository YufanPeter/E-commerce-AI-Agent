import Foundation

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
    let evidence: [EvidencePayload]?
    let updatedAt: Date?
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

struct ProductComparisonRequest: Codable, Hashable {
    let productIDs: [String]
    let focus: String?
}

/// 对比表里的一个商品表头（轻量，与后端 /compare 的 products[] 对齐）。
struct ComparisonProductPayload: Codable, Hashable, Identifiable {
    let productID: String
    let title: String
    let brand: String?
    let price: Double
    let imageURL: URL?

    var id: String { productID }

    enum CodingKeys: String, CodingKey {
        case productID = "product_id"
        case title
        case brand
        case price
        case imageURL = "image_url"
    }
}

/// 对比表的一行维度：label + 各商品的值（按 products 顺序）+ 更优者序号。
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
    let clientMessageID: String
    let text: String
    let attachments: [AttachmentPayload]
    let mode: AgentRequestMode?
    let profile: UserProfilePayload?
    let cart: CartSnapshotPayload?
    let locale: String
    let timezone: String
    /// 拍照找货：图片的 base64（可带 data URI 前缀）。纯文本请求为 nil。
    let imageBase64: String?

    init(
        sessionID: String? = nil,
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
    case products
    case comparison
    case clarification
    case specSelection
    case toolAction
    case cartSnapshot
    case error
    case done
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
    let error: APIErrorPayload?
    let traceID: String?

    init(
        id: String = UUID().uuidString,
        type: AgentStreamEventType,
        textDelta: String? = nil,
        status: AgentStatusPayload? = nil,
        products: [ProductPayload] = [],
        comparison: ProductComparisonPayload? = nil,
        clarification: ClarificationPromptPayload? = nil,
        specSelection: SpecSelection? = nil,
        toolAction: AgentToolActionPayload? = nil,
        cartSnapshot: CartSnapshotPayload? = nil,
        error: APIErrorPayload? = nil,
        traceID: String? = nil
    ) {
        self.id = id
        self.type = type
        self.textDelta = textDelta
        self.status = status
        self.products = products
        self.comparison = comparison
        self.clarification = clarification
        self.specSelection = specSelection
        self.toolAction = toolAction
        self.cartSnapshot = cartSnapshot
        self.error = error
        self.traceID = traceID
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
