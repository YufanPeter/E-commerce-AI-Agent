import Foundation

protocol AgentServicing {
    func streamResponse(
        for request: AgentRequestPayload
    ) -> AsyncThrowingStream<AgentStreamEventPayload, Error>

    func cancel(sessionID: String) async
}

protocol ProductServicing {
    func fetchProduct(productID: String) async throws -> ProductPayload
    func searchProducts(_ request: ProductSearchRequest) async throws -> ProductSearchResponse
    func compareProducts(_ request: ProductComparisonRequest) async throws -> ProductComparisonPayload
}

protocol CartServicing {
    func fetchCart() async throws -> CartSnapshotPayload
    func mutateCart(_ request: CartMutationRequest) async throws -> CartSnapshotPayload
    func createOrderPreview(selectedCartItemIDs: [String]) async throws -> OrderPreviewPayload
    func confirmOrder(_ request: OrderConfirmRequest) async throws -> OrderConfirmationPayload
}

protocol UserProfileServicing {
    func fetchProfile() async throws -> UserProfilePayload
    func updateProfile(_ request: UserProfileUpdateRequest) async throws -> UserProfilePayload
    func fetchAddresses() async throws -> [AddressPayload]
}

protocol MediaUploadServicing {
    func createUploadTicket(
        intent: UploadIntent,
        mimeType: String,
        sizeBytes: Int
    ) async throws -> UploadTicketPayload

    func uploadAttachment(
        localURL: URL,
        ticket: UploadTicketPayload,
        kind: AttachmentKind,
        mimeType: String
    ) async throws -> AttachmentPayload
}

protocol AnalyticsServicing {
    func track(_ event: AnalyticsEventPayload) async
}

struct FrontendServiceContainer {
    let agent: any AgentServicing
    let products: any ProductServicing
    let cart: any CartServicing
    let profile: any UserProfileServicing
    let mediaUpload: any MediaUploadServicing
    let analytics: any AnalyticsServicing

    static func mock() -> FrontendServiceContainer {
        FrontendServiceContainer(
            agent: MockAgentService(),
            products: MockProductService(),
            cart: MockCartService(),
            profile: MockUserProfileService(),
            mediaUpload: MockMediaUploadService(),
            analytics: MockAnalyticsService()
        )
    }
}

enum RESTServiceError: Error {
    case invalidURL
    case invalidResponse
    case requestFailed(statusCode: Int, code: String, message: String)
    case connectionFailed
    case decodingFailed

    var code: String {
        switch self {
        case .invalidURL:
            return "API_INVALID_URL"
        case .invalidResponse:
            return "API_INVALID_RESPONSE"
        case let .requestFailed(_, code, _):
            return code
        case .connectionFailed:
            return "API_CONNECTION_FAILED"
        case .decodingFailed:
            return "API_DECODING_FAILED"
        }
    }

    var message: String {
        switch self {
        case .invalidURL:
            return "商品服务地址无效。"
        case .invalidResponse:
            return "商品服务返回了无效响应。"
        case let .requestFailed(statusCode, _, message):
            return "\(message)（HTTP \(statusCode)）"
        case .connectionFailed:
            return "商品服务暂时不可用，请确认后端 REST API 已启动。"
        case .decodingFailed:
            return "商品服务返回数据解析失败。"
        }
    }

    var displayMessage: String {
        "\(message)\n错误码：\(code)"
    }
}

final class RESTProductService: ProductServicing {
    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder

    init(
        baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        self.session = session

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        self.decoder = decoder
    }

    func fetchProduct(productID: String) async throws -> ProductPayload {
        try await get(ProductPayload.self, path: "products/\(productID)")
    }

    func fetchProducts(productIDs: [String]) async throws -> [ProductPayload] {
        guard var components = URLComponents(
            url: baseURL.appendingPathComponent("products"),
            resolvingAgainstBaseURL: false
        ) else {
            throw RESTServiceError.invalidURL
        }
        components.queryItems = [
            URLQueryItem(name: "ids", value: productIDs.joined(separator: ","))
        ]
        guard let url = components.url else {
            throw RESTServiceError.invalidURL
        }
        return try await request(ProductSearchResponse.self, url: url).products
    }

    func searchProducts(_ request: ProductSearchRequest) async throws -> ProductSearchResponse {
        let productIDs = extractProductIDs(from: request.query)
        let products = try await fetchProducts(productIDs: productIDs)
        return ProductSearchResponse(
            requestID: UUID().uuidString,
            products: Array(products.prefix(request.limit)),
            evidence: products.flatMap { $0.evidence ?? [] }
        )
    }

    func compareProducts(_ request: ProductComparisonRequest) async throws -> ProductComparisonPayload {
        let products = try await fetchProducts(productIDs: request.productIDs)
        guard !products.isEmpty else {
            throw RESTServiceError.invalidResponse
        }
        let priceRow = ProductComparisonRow(
            id: "price",
            label: "价格",
            values: products.enumerated().map { index, product in
                ProductComparisonValue(
                    productID: product.productID,
                    text: product.price.display,
                    isHighlighted: index == 0,
                    evidence: product.evidence ?? []
                )
            }
        )
        return ProductComparisonPayload(
            title: "商品对比",
            products: products,
            rows: [priceRow],
            recommendation: products.first?.title ?? ""
        )
    }

    private func get<T: Decodable>(_ type: T.Type, path: String) async throws -> T {
        let url = baseURL.appendingPathComponent(path)
        return try await request(type, url: url)
    }

    private func request<T: Decodable>(_ type: T.Type, url: URL) async throws -> T {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(from: url)
        } catch {
            throw RESTServiceError.connectionFailed
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw RESTServiceError.invalidResponse
        }
        guard (200..<300).contains(httpResponse.statusCode) else {
            let payload = decodeErrorPayload(from: data)
            throw RESTServiceError.requestFailed(
                statusCode: httpResponse.statusCode,
                code: payload?.code ?? "API_HTTP_\(httpResponse.statusCode)",
                message: payload?.message ?? "商品服务请求失败。"
            )
        }
        do {
            return try decoder.decode(type, from: data)
        } catch {
            throw RESTServiceError.decodingFailed
        }
    }

    private func decodeErrorPayload(from data: Data) -> APIErrorPayload? {
        if let payload = try? decoder.decode(APIErrorPayload.self, from: data) {
            return payload
        }
        if let envelope = try? decoder.decode(APIErrorEnvelope.self, from: data) {
            return envelope.detail
        }
        return nil
    }

    private func extractProductIDs(from text: String) -> [String] {
        let pattern = #"p_(beauty|clothes|digital|food)_\d{3}"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else {
            return []
        }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.matches(in: text, range: range).compactMap { match in
            Range(match.range, in: text).map { String(text[$0]) }
        }
    }
}

private struct APIErrorEnvelope: Decodable {
    let detail: APIErrorPayload?
}

extension Product {
    init(payload: ProductPayload) {
        let specifications = payload.specifications.map { specification in
            ProductSpecification(
                name: specification.name,
                options: specification.options.map(\.label)
            )
        }

        let skus = payload.skus.map { sku in
            ProductSKU(
                id: sku.id,
                selectedOptions: sku.selectedOptions,
                price: Double(sku.price.amountMinor) / 100,
                priceDisplay: sku.price.display
            )
        }

        self.init(
            id: payload.productID,
            title: payload.title,
            price: payload.price.display,
            reason: "",
            details: "",
            tags: payload.tags,
            specifications: specifications,
            imageURL: payload.imageURL,
            skus: skus
        )
    }
}
