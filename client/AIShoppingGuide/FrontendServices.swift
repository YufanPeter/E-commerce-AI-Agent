import Foundation
import SwiftUI

/// Centralized backend connection configuration for switching to a LAN address during device testing.
///
/// - iOS Simulator: keep the default `http://127.0.0.1:8000`, which shares the Mac's localhost.
/// - Physical iOS device: start the backend with `HOST=0.0.0.0 ./scripts/start_backend.sh`,
///   then change `defaultBaseURL` below to the Mac's LAN address, for example
///   `http://192.168.1.23:8000`. The startup script prints this address when ready.
enum BackendConfig {
    static let defaultBaseURL: URL = {
        if let env = ProcessInfo.processInfo.environment["BACKEND_BASE_URL"],
           let url = URL(string: env) {
            return url
        }

        #if targetEnvironment(simulator)
        return URL(string: "http://127.0.0.1:8000")!
        #else
        return URL(string: "http://192.168.1.23:8000")!
        #endif
    }()

    /// Health-check endpoint.
    static var healthURL: URL { defaultBaseURL.appendingPathComponent("health") }

    /// Cart reset endpoint used to clear backend state when required.
    static var cartResetURL: URL { defaultBaseURL.appendingPathComponent("cart/reset") }
}

/// Backend reachability state.
enum BackendReachability: Equatable {
    case unknown
    case checking
    case reachable
    case unreachable
}

/// Polls `/health` at startup and afterward, exposing reachability to the UI so the app can
/// provide immediate guidance instead of waiting for a message request to fail.
@MainActor
final class BackendHealthMonitor: ObservableObject {
    @Published private(set) var status: BackendReachability = .unknown

    private let healthURL: URL
    private let session: URLSession
    private var pollTask: Task<Void, Never>?

    init(healthURL: URL = BackendConfig.healthURL, session: URLSession = .shared) {
        self.healthURL = healthURL
        self.session = session
    }

    /// Performs an immediate backend readiness check.
    @discardableResult
    func check() async -> BackendReachability {
        status = .checking
        let result = await probe()
        status = result
        return result
    }

    /// Starts periodic polling: every 3 seconds while unreachable and every 15 seconds after recovery.
    func startMonitoring() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let result = await self.probe()
                await MainActor.run { self.status = result }
                let interval: UInt64 = (result == .reachable) ? 15 : 3
                try? await Task.sleep(nanoseconds: interval * 1_000_000_000)
            }
        }
    }

    func stopMonitoring() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func probe() async -> BackendReachability {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 4
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                return .unreachable
            }
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let statusValue = json["status"] as? String, statusValue == "ok" {
                return .reachable
            }
            return .reachable
        } catch {
            return .unreachable
        }
    }
}

protocol AgentServicing {
    func streamResponse(
        for request: AgentRequestPayload
    ) -> AsyncThrowingStream<AgentStreamEventPayload, Error>

    func cancel(sessionID: String) async
}

protocol ProductServicing {
    func fetchProduct(productID: String) async throws -> ProductPayload
    func fetchProducts(productIDs: [String]) async throws -> [ProductPayload]
    func fetchProductPitch(productID: String) async -> String?
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

protocol PreferenceServicing {
    func fetchPreference(userID: String) async throws -> UserPreferencePayload
    func updatePreference(_ preference: UserPreferencePayload) async throws -> UserPreferencePayload
    func undoPreference(userID: String, undoToken: String) async throws -> UserPreferencePayload
    func fetchMemory(userID: String) async throws -> MemoryViewPayload
    func tellMemory(userID: String, text: String) async throws -> MemoryActionResult
    func confirmMemory(userID: String, itemID: String) async throws -> MemoryActionResult
    func rejectMemory(userID: String, itemID: String) async throws -> MemoryActionResult
    func deleteMemory(userID: String, itemID: String) async throws -> MemoryActionResult
    func undoMemory(userID: String, undoToken: String) async throws -> MemoryActionResult
    func setPersonalization(userID: String, enabled: Bool) async throws -> MemoryViewPayload
    func clearMemory(userID: String) async throws -> MemoryViewPayload
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
    let preference: any PreferenceServicing
    let mediaUpload: any MediaUploadServicing
    let analytics: any AnalyticsServicing

    static func mock() -> FrontendServiceContainer {
        FrontendServiceContainer(
            agent: MockAgentService(),
            products: MockProductService(),
            cart: MockCartService(),
            profile: MockUserProfileService(),
            preference: MockPreferenceService(),
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
            return "The product service URL is invalid."
        case .invalidResponse:
            return "The product service returned an invalid response."
        case let .requestFailed(statusCode, _, message):
            return "\(message)（HTTP \(statusCode)）"
        case .connectionFailed:
            return "The product service is unavailable. Make sure the backend is running."
        case .decodingFailed:
            return "Could not read the product service response."
        }
    }

    var displayMessage: String {
        "\(message)\nError code: \(code)"
    }
}

final class RESTProductService: ProductServicing {
    private let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder

    init(
        baseURL: URL = BackendConfig.defaultBaseURL,
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

    /// Fetches supplemental product-detail copy using the shopping-guide composer; returns nil on failure.
    func fetchProductPitch(productID: String) async -> String? {
        var request = URLRequest(url: baseURL.appendingPathComponent("products/\(productID)/pitch"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 20
        guard
            let (data, response) = try? await session.data(for: request),
            let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let raw = obj["description"] as? String
        else {
            return nil
        }
        let cleaned = RecommendationCopy.sanitized(raw)
        return cleaned.isEmpty ? nil : cleaned
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
        if productIDs.isEmpty {
            let url = baseURL.appendingPathComponent("products").appendingPathComponent("search")
            guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
                throw RESTServiceError.invalidURL
            }
            components.queryItems = [
                URLQueryItem(name: "q", value: request.query),
                URLQueryItem(name: "limit", value: String(max(1, min(request.limit, 24))))
            ]
            guard let endpoint = components.url else { throw RESTServiceError.invalidURL }
            return try await self.request(ProductSearchResponse.self, url: endpoint)
        }
        let products = try await fetchProducts(productIDs: productIDs)
        return ProductSearchResponse(
            requestID: UUID().uuidString,
            products: Array(products.prefix(request.limit)),
            evidence: products.flatMap { $0.evidence ?? [] }
        )
    }

    func compareProducts(_ request: ProductComparisonRequest) async throws -> ProductComparisonPayload {
        var urlRequest = URLRequest(url: baseURL.appendingPathComponent("compare"))
        urlRequest.httpMethod = "POST"
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["product_ids": request.productIDs]
        if let items = request.items, !items.isEmpty {
            body["items"] = items.map { item -> [String: Any] in
                var entry: [String: Any] = ["product_id": item.productID]
                if let skuID = item.skuID { entry["sku_id"] = skuID }
                return entry
            }
        }
        if let budget = request.budgetMax { body["budget_max"] = budget }
        if let focus = request.focus, !focus.isEmpty { body["focus"] = focus }
        urlRequest.httpBody = try JSONSerialization.data(withJSONObject: body)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: urlRequest)
        } catch {
            throw RESTServiceError.connectionFailed
        }
        guard let http = response as? HTTPURLResponse else {
            throw RESTServiceError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let payload = decodeErrorPayload(from: data)
            throw RESTServiceError.requestFailed(
                statusCode: http.statusCode,
                code: payload?.code ?? "API_HTTP_\(http.statusCode)",
                message: payload?.message ?? "Could not compare products."
            )
        }
        do {
            return try decoder.decode(ProductComparisonPayload.self, from: data)
        } catch {
            throw RESTServiceError.decodingFailed
        }
    }

    private func get<T: Decodable>(_ type: T.Type, path: String) async throws -> T {
        let url = baseURL.appendingPathComponent(path)
        return try await request(type, url: url)
    }

    /// Fetches the current backend cart snapshot with `GET /cart` for cold-launch restoration.
    /// Uses the same lines-to-items parser as SSE cart snapshots to keep rendering consistent.
    func fetchAgentCart() async throws -> CartSnapshotPayload {
        let url = baseURL.appendingPathComponent("cart")
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(from: url)
        } catch {
            throw RESTServiceError.connectionFailed
        }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw RESTServiceError.invalidResponse
        }
        return await agentCartSnapshot(from: data)
    }

    func mutateAgentCart(_ mutation: CartMutationRequest) async throws -> CartSnapshotPayload {
        var urlRequest = URLRequest(url: baseURL.appendingPathComponent("cart/mutate"))
        urlRequest.httpMethod = "POST"
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            urlRequest.httpBody = try JSONEncoder().encode(mutation)
        } catch {
            throw RESTServiceError.invalidResponse
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: urlRequest)
        } catch {
            throw RESTServiceError.connectionFailed
        }
        guard let http = response as? HTTPURLResponse else {
            throw RESTServiceError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let payload = decodeErrorPayload(from: data)
            throw RESTServiceError.requestFailed(
                statusCode: http.statusCode,
                code: payload?.code ?? "API_HTTP_\(http.statusCode)",
                message: payload?.message ?? "Could not update the cart."
            )
        }
        return await agentCartSnapshot(from: data)
    }

    /// 购物车页下单：后端写入订单并移出已下单的行。
    func checkoutAgentCart(
        userID: String = UserIdentity.defaultUserID,
        cartItemIDs: [String]
    ) async throws -> (order: PlacedOrderPayload, cart: CartSnapshotPayload) {
        var urlRequest = URLRequest(url: baseURL.appendingPathComponent("cart/checkout"))
        urlRequest.httpMethod = "POST"
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        urlRequest.httpBody = try JSONSerialization.data(withJSONObject: [
            "user_id": userID,
            "cart_item_ids": cartItemIDs,
        ] as [String: Any])
        let (data, response) = try await session.data(for: urlRequest)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw RESTServiceError.invalidResponse
        }
        let order = try JSONDecoder().decode(PlacedOrderEnvelope.self, from: data).order
        return (order, await agentCartSnapshot(from: data))
    }

    private func agentCartSnapshot(from data: Data) async -> CartSnapshotPayload {
        guard
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let cart = obj["cart"] as? [String: Any],
            let lines = cart["lines"] as? [[String: Any]],
            !lines.isEmpty
        else {
            return Self.emptyAgentCart()
        }
        let productIDs = lines.compactMap { $0["product_id"] as? String }
        let productPayloads = (try? await fetchProducts(productIDs: productIDs)) ?? []
        let productsByID = productPayloads.reduce(into: [String: ProductPayload]()) { acc, p in
            acc[p.productID] = p
        }
        let items = lines.compactMap { line -> CartItemPayload? in
            guard
                let productID = line["product_id"] as? String,
                let product = productsByID[productID]
            else { return nil }
            let cartItemID = String(describing: line["cart_item_id"] ?? UUID().uuidString)
            let selectedOptions = line["options"] as? [String: String] ?? [:]
            let quantity = (line["quantity"] as? Int) ?? 1
            let selected = (line["selected"] as? Bool) ?? true
            let subtotal = (line["subtotal"] as? Double) ?? Double((line["subtotal"] as? Int) ?? 0)
            return CartItemPayload(
                id: cartItemID,
                productID: productID,
                skuID: line["sku_id"] as? String,
                product: product,
                selectedOptions: selectedOptions,
                quantity: quantity,
                isSelected: selected,
                lineTotal: Money(currency: "CNY", amountMinor: Int((subtotal * 100).rounded()), display: "¥\(subtotal)")
            )
        }
        let total = (cart["total"] as? Double) ?? Double((cart["total"] as? Int) ?? 0)
        return CartSnapshotPayload(
            cartID: "agent-cart",
            items: items,
            selectedItemIDs: items.filter { $0.isSelected }.map { $0.id },
            priceSummary: CartPriceSummaryPayload(
                subtotal: Money(currency: "CNY", amountMinor: Int((total * 100).rounded()), display: "¥\(total)"),
                discount: nil,
                payable: Money(currency: "CNY", amountMinor: Int((total * 100).rounded()), display: "¥\(total)")
            ),
            updatedAt: Date()
        )
    }

    private static func emptyAgentCart() -> CartSnapshotPayload {
        CartSnapshotPayload(
            cartID: "agent-cart",
            items: [],
            selectedItemIDs: [],
            priceSummary: CartPriceSummaryPayload(
                subtotal: Money(currency: "CNY", amountMinor: 0, display: "¥0"),
                discount: nil,
                payable: Money(currency: "CNY", amountMinor: 0, display: "¥0")
            ),
            updatedAt: Date()
        )
    }

    /// Generates a concise title from the first exchange with `POST /title`; returns nil on failure.
    func fetchTitle(userText: String, assistantText: String?) async -> String? {
        var request = URLRequest(url: baseURL.appendingPathComponent("title"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        var body: [String: Any] = ["user_text": userText]
        if let assistantText, !assistantText.isEmpty { body["assistant_text"] = assistantText }
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return nil }
        request.httpBody = data
        guard
            let (respData, response) = try? await session.data(for: request),
            let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
            let obj = try? JSONSerialization.jsonObject(with: respData) as? [String: Any],
            let title = obj["title"] as? String,
            !title.trimmingCharacters(in: .whitespaces).isEmpty
        else {
            return nil
        }
        return title
    }

    /// Fetches empty-state suggestions with `GET /suggestions`: category shortcuts and inventory-backed trending searches.
    func fetchSuggestions() async -> HomeSuggestions? {
        let url = baseURL.appendingPathComponent("suggestions")
        guard
            let (data, response) = try? await session.data(from: url),
            let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return nil
        }
        let categories = obj["categories"] as? [String] ?? []
        let hot = obj["hot_searches"] as? [String] ?? []
        guard !categories.isEmpty || !hot.isEmpty else { return nil }
        return HomeSuggestions(categories: categories, hotSearches: hot)
    }

    func fetchHomeFeed(userID: String = UserIdentity.defaultUserID) async -> HomeFeed? {
        let url = baseURL.appendingPathComponent("home").appendingPathComponent(userID)
        guard
            let (data, response) = try? await session.data(from: url),
            let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return nil
        }
        let entries = (obj["entries"] as? [[String: Any]] ?? []).compactMap { raw -> HomeFeed.Entry? in
            guard let title = raw["title"] as? String, let query = raw["query"] as? String else { return nil }
            return HomeFeed.Entry(
                title: title,
                query: query,
                imageURL: (raw["image_url"] as? String).flatMap(URL.init(string:))
            )
        }
        return HomeFeed(isReturning: obj["returning"] as? Bool ?? false, entries: entries)
    }

    /// 提交对回答的评价。返回是否成功，失败时界面撤回选中状态。
    func submitFeedback(
        messageID: String,
        rating: String,
        sessionID: String,
        query: String,
        answer: String,
        userID: String = UserIdentity.defaultUserID
    ) async -> Bool {
        var request = URLRequest(url: baseURL.appendingPathComponent("feedback"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = [
            "message_id": messageID,
            "rating": rating,
            "session_id": sessionID,
            "user_id": userID,
            "query": query,
            "answer": answer,
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard
            let (_, response) = try? await session.data(for: request),
            let http = response as? HTTPURLResponse
        else {
            return false
        }
        return (200..<300).contains(http.statusCode)
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
                message: payload?.message ?? "The product service request failed."
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
        let pattern = #"\b(?:p_(?:beauty|clothes|digital|food)_\d{3}|amz_[A-Z0-9]{10})\b"#
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

private struct PreferenceUndoResponse: Codable {
    let preference: UserPreferencePayload
}

final class RESTPreferenceService: PreferenceServicing {
    private let baseURL: URL
    private let session: URLSession
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    init(baseURL: URL = BackendConfig.defaultBaseURL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func fetchPreference(userID: String = UserIdentity.defaultUserID) async throws -> UserPreferencePayload {
        let url = baseURL.appendingPathComponent("preferences/\(userID)")
        let (data, response) = try await session.data(from: url)
        try validate(response: response, data: data, fallback: "Could not load preferences.")
        return try decoder.decode(UserPreferencePayload.self, from: data)
    }

    func updatePreference(_ preference: UserPreferencePayload) async throws -> UserPreferencePayload {
        var request = URLRequest(url: baseURL.appendingPathComponent("preferences/\(preference.userID)"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(preference)
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data, fallback: "Could not save preferences.")
        return try decoder.decode(UserPreferencePayload.self, from: data)
    }

    func undoPreference(userID: String, undoToken: String) async throws -> UserPreferencePayload {
        var request = URLRequest(url: baseURL.appendingPathComponent("preferences/\(userID)/undo"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(PreferenceUndoRequest(undoToken: undoToken))
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data, fallback: "Could not undo the preference change.")
        return try decoder.decode(PreferenceUndoResponse.self, from: data).preference
    }

    func fetchMemory(userID: String) async throws -> MemoryViewPayload {
        try await memoryRequest("memory/\(userID)", method: "GET")
    }

    func tellMemory(userID: String, text: String) async throws -> MemoryActionResult {
        try await memoryRequest("memory/\(userID)/tell", method: "POST", body: ["text": text])
    }

    func confirmMemory(userID: String, itemID: String) async throws -> MemoryActionResult {
        try await memoryRequest("memory/\(userID)/items/\(itemID)/confirm", method: "POST")
    }

    func rejectMemory(userID: String, itemID: String) async throws -> MemoryActionResult {
        try await memoryRequest("memory/\(userID)/items/\(itemID)/reject", method: "POST")
    }

    func deleteMemory(userID: String, itemID: String) async throws -> MemoryActionResult {
        try await memoryRequest("memory/\(userID)/items/\(itemID)", method: "DELETE")
    }

    func undoMemory(userID: String, undoToken: String) async throws -> MemoryActionResult {
        try await memoryRequest("memory/\(userID)/undo", method: "POST", body: ["undo_token": undoToken])
    }

    func setPersonalization(userID: String, enabled: Bool) async throws -> MemoryViewPayload {
        var request = URLRequest(url: baseURL.appendingPathComponent("memory/\(userID)/settings"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["personalization_enabled": enabled])
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data, fallback: "Could not save settings.")
        return try decoder.decode(MemoryViewPayload.self, from: data)
    }

    func clearMemory(userID: String) async throws -> MemoryViewPayload {
        try await memoryRequest("memory/\(userID)", method: "DELETE")
    }

    private func memoryRequest<T: Decodable>(_ path: String, method: String, body: [String: String]? = nil) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try encoder.encode(body)
        }
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data, fallback: "The memory service request failed.")
        return try decoder.decode(T.self, from: data)
    }

    private func validate(response: URLResponse, data: Data, fallback: String) throws {
        guard let http = response as? HTTPURLResponse else {
            throw RESTServiceError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let payload = decodePreferenceError(from: data)
            throw RESTServiceError.requestFailed(
                statusCode: http.statusCode,
                code: payload?.code ?? "API_HTTP_\(http.statusCode)",
                message: payload?.message ?? fallback
            )
        }
    }

    private func decodePreferenceError(from data: Data) -> APIErrorPayload? {
        if let payload = try? decoder.decode(APIErrorPayload.self, from: data) {
            return payload
        }
        if let envelope = try? decoder.decode(APIErrorEnvelope.self, from: data) {
            return envelope.detail
        }
        return nil
    }
}

/// Thread-safe storage for reusing the session ID across turns.
private actor SessionIDStore {
    private var sessionID: String?
    func current() -> String? { sessionID }
    func update(_ value: String) { sessionID = value }
}

/// Production agent service that connects to `/chat/stream` over SSE and drives the LLM and RAG pipeline.
///
/// 后端事件 → 客户端事件映射：
///   - `session`     → 记录 session_id（多轮上下文复用，不向 UI 透出）
///   - `status`      → 阶段提示（routing→understanding / tool→retrieving / compose→generating）
///   - `tool_result` → 取出商品 ID，调 `/products` 补全完整数据后以 `.products` 透出
///   - `narrative`   → 结构化流式片段：opening(逐字) / item(单款解说) / summary(结尾总结) / followup(单条追问)
///                     → `.openingDelta` / `.itemDescription` / `.summary` / `.followup`
///   - `token`       → 文本增量（纯文本回答逐 token；结构化回答末尾附带一份完整 JSON，兼容用）
///   - `done`        → 结束
///   - `error`       → 抛出 RESTServiceError
final class RESTAgentService: AgentServicing {
    private let baseURL: URL
    private let productService: RESTProductService
    private let sessionStore = SessionIDStore()

    init(baseURL: URL = BackendConfig.defaultBaseURL) {
        self.baseURL = baseURL
        self.productService = RESTProductService(baseURL: baseURL)
    }

    /// Allows a longer streaming timeout because the first request may spend about 60 seconds loading the embedding model.
    private static func makeStreamingConfig() -> URLSessionConfiguration {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 300
        config.waitsForConnectivity = true
        // Disable caching so the local cache layer does not buffer the SSE response.
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        return config
    }

    func streamResponse(
        for request: AgentRequestPayload
    ) -> AsyncThrowingStream<AgentStreamEventPayload, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    try await self.run(request: request, continuation: continuation)
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    func cancel(sessionID: String) async {}

    private func run(
        request payload: AgentRequestPayload,
        continuation: AsyncThrowingStream<AgentStreamEventPayload, Error>.Continuation
    ) async throws {
        var urlRequest = URLRequest(url: baseURL.appendingPathComponent("chat/stream"))
        urlRequest.httpMethod = "POST"
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        urlRequest.setValue("text/event-stream", forHTTPHeaderField: "Accept")

        let storedSession = await sessionStore.current()
        let resolvedSession = payload.sessionID ?? storedSession
        var body: [String: Any] = ["query": payload.text, "user_id": payload.userID]
        if let resolvedSession { body["session_id"] = resolvedSession }
        if let image = payload.imageBase64 { body["image_base64"] = image }
        urlRequest.httpBody = try JSONSerialization.data(withJSONObject: body)

        // Parse SSE incrementally with URLSessionDataDelegate. On iOS, URLSession.AsyncBytes can
        // buffer streamed responses and leave the UI in an intermediate state. The delegate's
        // didReceive(data:) callback delivers each network chunk immediately and is more reliable.
        let config = Self.makeStreamingConfig()
        let (rawStream, rawCont) = AsyncThrowingStream<SSERawEvent, Error>.makeStream()
        let delegate = SSEDelegate(continuation: rawCont)
        let urlSession = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        let task = urlSession.dataTask(with: urlRequest)
        rawCont.onTermination = { _ in task.cancel() }
        task.resume()

        do {
            for try await raw in rawStream {
                try Task.checkCancellation()
                try await dispatch(event: raw.event, data: raw.data, continuation: continuation)
            }
        } catch {
            task.cancel()
            urlSession.invalidateAndCancel()
            throw error
        }
        urlSession.finishTasksAndInvalidate()
    }

    private func dispatch(
        event: String,
        data: String,
        continuation: AsyncThrowingStream<AgentStreamEventPayload, Error>.Continuation
    ) async throws {
        let bytes = Data(data.utf8)

        switch event {
        case "session":
            if let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
               let sid = obj["session_id"] as? String {
                await sessionStore.update(sid)
            }

        case "status":
            guard let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { return }
            let phaseRaw = obj["phase"] as? String ?? ""
            let message = obj["message"] as? String ?? ""
            let phase = Self.mapPhase(phaseRaw)
            continuation.yield(
                AgentStreamEventPayload(
                    type: .status,
                    status: AgentStatusPayload(phase: phase, message: message)
                )
            )

        case "tool_result":
            // If the backend requests a variant, emit an interactive selection card and stop
            // parsing products or cart state because no item has been added yet.
            if let spec = Self.extractSpecSelection(fromToolResult: bytes) {
                continuation.yield(
                    AgentStreamEventPayload(type: .specSelection, specSelection: spec)
                )
                return
            }
            // Convert a structured comparison-tool result into a renderable comparison card.
            if let comparison = Self.extractComparison(fromToolResult: bytes) {
                continuation.yield(
                    AgentStreamEventPayload(type: .comparison, comparison: comparison)
                )
                return
            }
            if Self.isCartToolResult(bytes) {
                // Await synchronously so the cart snapshot is emitted before `done`. When an add
                // returns quickly (`needs_composer=false`), a detached task might otherwise be
                // terminated before product loading finishes, producing an empty cart or mismatched prices.
                if let snapshot = try? await Self.extractCartSnapshot(
                    fromToolResult: bytes,
                    productService: productService
                ) {
                    continuation.yield(
                        AgentStreamEventPayload(type: .cartSnapshot, cartSnapshot: snapshot)
                    )
                }
                return  // cart 结果不含 products[]，无需再走商品补全
            }
            let ids = Self.extractProductIDs(fromToolResult: bytes)
            guard !ids.isEmpty else { return }
            let budgets = Self.extractBudgetHints(fromToolResult: bytes)
            // 补全：Agent 只给精简卡片，这里用商品 ID 取完整数据用于渲染。
            if let payloads = try? await productService.fetchProducts(productIDs: ids), !payloads.isEmpty {
                continuation.yield(AgentStreamEventPayload(type: .products, budgets: budgets, products: payloads))
            }

        case "narrative":
            // 结构化流式片段：后端 composer 边生成边按"开场白 → 商品解说 → 追问"顺序下发。
            guard let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                  let part = obj["part"] as? String else { return }
            switch part {
            case "opening":
                if let delta = obj["delta"] as? String, !delta.isEmpty {
                    continuation.yield(AgentStreamEventPayload(type: .openingDelta, textDelta: delta))
                }
            case "item":
                if let pid = obj["productId"] as? String, !pid.isEmpty {
                    let description = obj["description"] as? String ?? ""
                    continuation.yield(
                        AgentStreamEventPayload(
                            type: .itemDescription,
                            textDelta: description,
                            productID: pid,
                            group: obj["group"] as? String
                        )
                    )
                }
            case "summary":
                if let text = obj["text"] as? String, !text.isEmpty {
                    continuation.yield(AgentStreamEventPayload(type: .summary, textDelta: text))
                }
            case "point":
                if let text = obj["text"] as? String, !text.isEmpty {
                    continuation.yield(AgentStreamEventPayload(type: .comparePoint, textDelta: text))
                }
            case "pick":
                if let text = obj["text"] as? String, !text.isEmpty {
                    continuation.yield(AgentStreamEventPayload(type: .comparePick, textDelta: text))
                }
            case "followup":
                if let text = obj["text"] as? String, !text.isEmpty {
                    continuation.yield(AgentStreamEventPayload(type: .followup, textDelta: text))
                }
            default:
                break  // opening_end / group：分区信息已随 item 带上，这里不需要单独处理
            }

        case "token":
            // `data` is a JSON-encoded string, for example `data: "some text"`.
            if let piece = try? JSONDecoder().decode(String.self, from: bytes), !piece.isEmpty {
                continuation.yield(AgentStreamEventPayload(type: .textDelta, textDelta: piece))
            } else if !data.isEmpty {
                continuation.yield(AgentStreamEventPayload(type: .textDelta, textDelta: data))
            }

        case "memory_update":
            if let update = try? JSONDecoder().decode(MemoryUpdatePayload.self, from: bytes) {
                continuation.yield(
                    AgentStreamEventPayload(type: .memoryUpdate, memoryUpdate: update)
                )
            }

        case "done":
            continuation.yield(AgentStreamEventPayload(type: .done))

        case "error":
            let message = (try? JSONSerialization.jsonObject(with: bytes) as? [String: Any])?["message"] as? String
            throw RESTServiceError.requestFailed(
                statusCode: 500,
                code: "AGENT_STREAM_ERROR",
                message: message ?? "The assistant stopped responding."
            )

        default:
            break  // meta 等调试事件忽略
        }
    }

    private static func mapPhase(_ raw: String) -> AgentStatusPhase {
        switch raw {
        case "routing": return .understanding
        case "tool": return .retrieving
        case "compose": return .generating
        case "done": return .done
        default: return .retrieving
        }
    }

    /// 从 tool_result 的 payload.products[].budget 里取预算匹配信息（用户给了预算时才有）。
    private static func extractBudgetHints(fromToolResult bytes: Data) -> [String: ProductBudgetHint] {
        guard
            let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
            let payload = obj["payload"] as? [String: Any],
            let products = payload["products"] as? [[String: Any]]
        else {
            return [:]
        }
        var hints: [String: ProductBudgetHint] = [:]
        for product in products {
            guard let pid = product["product_id"] as? String,
                  let budget = product["budget"] as? [String: Any] else { continue }
            hints[pid] = ProductBudgetHint(
                maxPrice: doubleValue(budget["max_price"]),
                minPrice: doubleValue(budget["min_price"]),
                skusWithinBudget: (budget["skus_within_budget"] as? Int) ?? 0,
                skuCount: (budget["sku_count"] as? Int) ?? 0,
                cheapestSKUID: budget["cheapest_sku_id"] as? String,
                cheapestPrice: doubleValue(budget["cheapest_price"])
            )
        }
        return hints
    }

    private static func doubleValue(_ value: Any?) -> Double? {
        if let d = value as? Double { return d }
        if let i = value as? Int { return Double(i) }
        if let n = value as? NSNumber { return n.doubleValue }
        return nil
    }

    /// 从 tool_result 的 payload.products[] 中按顺序抽取 product_id。
    private static func extractProductIDs(fromToolResult bytes: Data) -> [String] {
        guard
            let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
            let payload = obj["payload"] as? [String: Any],
            let products = payload["products"] as? [[String: Any]]
        else {
            return []
        }
        return products.compactMap { $0["product_id"] as? String }
    }

    private static func isCartToolResult(_ bytes: Data) -> Bool {
        guard
            let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
            (obj["tool_name"] as? String) == "cart"
        else {
            return false
        }
        return true
    }

    /// Parses the data required for a variant-selection card from a cart tool's `ask_spec` result.
    /// Returns a value only when `action == ask_spec` and ordered `dimensions` are present.
    private static func extractSpecSelection(fromToolResult bytes: Data) -> SpecSelection? {
        guard
            let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
            (obj["tool_name"] as? String) == "cart",
            let payload = obj["payload"] as? [String: Any],
            (payload["action"] as? String) == "ask_spec",
            let productID = payload["product_id"] as? String,
            let rawDimensions = payload["dimensions"] as? [[String: Any]]
        else {
            return nil
        }
        let title = payload["title"] as? String ?? ""
        let dimensions = rawDimensions.compactMap { dim -> SpecDimension? in
            guard
                let name = dim["name"] as? String,
                let values = dim["values"] as? [String], !values.isEmpty
            else {
                return nil
            }
            return SpecDimension(name: name, values: values)
        }
        guard !dimensions.isEmpty else { return nil }
        return SpecSelection(productID: productID, title: title, dimensions: dimensions)
    }

    /// Parses comparison-card data from a compare-tool result. Returns nil when `comparison` is null,
    /// such as when too few products were identified, so the conversation can fall back to plain text.
    private static func extractComparison(fromToolResult bytes: Data) -> ProductComparisonPayload? {
        guard
            let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
            (obj["tool_name"] as? String) == "compare",
            let payload = obj["payload"] as? [String: Any],
            let comparison = payload["comparison"] as? [String: Any]
        else {
            return nil
        }
        guard let data = try? JSONSerialization.data(withJSONObject: comparison) else {
            return nil
        }
        return try? JSONDecoder().decode(ProductComparisonPayload.self, from: data)
    }

    private static func extractCartSnapshot(
        fromToolResult bytes: Data,
        productService: RESTProductService
    ) async throws -> CartSnapshotPayload? {
        guard
            let obj = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
            (obj["tool_name"] as? String) == "cart",
            let payload = obj["payload"] as? [String: Any]
        else {
            return nil
        }

        let action = payload["action"] as? String
        if action == "checkout" {
            return emptyCartSnapshot()
        }

        guard
            let cart = payload["cart"] as? [String: Any],
            let lines = cart["lines"] as? [[String: Any]]
        else {
            return nil
        }
        if lines.isEmpty {
            return emptyCartSnapshot()
        }

        let productIDs = lines.compactMap { $0["product_id"] as? String }
        let productPayloads = try await productService.fetchProducts(productIDs: productIDs)
        let productsByID = productPayloads.reduce(into: [String: ProductPayload]()) { result, product in
            result[product.productID] = product
        }

        let items = lines.compactMap { line -> CartItemPayload? in
            guard
                let productID = line["product_id"] as? String,
                let product = productsByID[productID]
            else {
                return nil
            }
            let cartItemID = String(describing: line["cart_item_id"] ?? UUID().uuidString)
            let selectedOptions = line["options"] as? [String: String] ?? [:]
            let quantity = intValue(line["quantity"], default: 1)
            let selected = boolValue(line["selected"], default: true)
            let subtotal = doubleValue(line["subtotal"], default: 0)
            return CartItemPayload(
                id: cartItemID,
                productID: productID,
                skuID: line["sku_id"] as? String,
                product: product,
                selectedOptions: selectedOptions,
                quantity: quantity,
                isSelected: selected,
                lineTotal: yuanMoney(subtotal)
            )
        }

        let total = doubleValue(cart["total"], default: items.reduce(0) {
            $0 + Double($1.lineTotal.amountMinor) / 100
        })
        return CartSnapshotPayload(
            cartID: "agent-cart",
            items: items,
            selectedItemIDs: items.filter { $0.isSelected }.map { $0.id },
            priceSummary: CartPriceSummaryPayload(
                subtotal: yuanMoney(total),
                discount: nil,
                payable: yuanMoney(total)
            ),
            updatedAt: Date()
        )
    }

    private static func emptyCartSnapshot() -> CartSnapshotPayload {
        CartSnapshotPayload(
            cartID: "agent-cart",
            items: [],
            selectedItemIDs: [],
            priceSummary: CartPriceSummaryPayload(
                subtotal: yuanMoney(0),
                discount: nil,
                payable: yuanMoney(0)
            ),
            updatedAt: Date()
        )
    }

    private static func yuanMoney(_ value: Double) -> Money {
        let cents = Int((value * 100).rounded())
        let display: String
        if value.rounded(.towardZero) == value {
            display = "¥\(Int(value))"
        } else {
            display = "¥\(String(format: "%.2f", value))"
        }
        return Money(currency: "CNY", amountMinor: cents, display: display)
    }

    private static func intValue(_ value: Any?, default defaultValue: Int) -> Int {
        if let int = value as? Int { return int }
        if let double = value as? Double { return Int(double) }
        if let string = value as? String, let int = Int(string) { return int }
        return defaultValue
    }

    private static func doubleValue(_ value: Any?, default defaultValue: Double) -> Double {
        if let double = value as? Double { return double }
        if let int = value as? Int { return Double(int) }
        if let string = value as? String, let double = Double(string) { return double }
        return defaultValue
    }

    private static func boolValue(_ value: Any?, default defaultValue: Bool) -> Bool {
        if let bool = value as? Bool { return bool }
        if let int = value as? Int { return int != 0 }
        if let string = value as? String { return string == "1" || string.lowercased() == "true" }
        return defaultValue
    }
}

/// A complete raw SSE event containing its event name and merged data text.
private struct SSERawEvent {
    let event: String
    let data: String
}

/// Incremental SSE parser built on `URLSessionDataDelegate`.
///
/// Each network chunk received by `didReceive(data:)` is scanned byte by byte for `\n`. Complete lines
/// are parsed as `event:` or `data:`, and a blank line yields the accumulated event to `AsyncThrowingStream`.
/// This keeps token and completion events real-time and avoids `URLSession.AsyncBytes` buffering.
private final class SSEDelegate: NSObject, URLSessionDataDelegate {
    private var buffer = Data()
    private var eventName = ""
    private var dataLines: [String] = []
    private let continuation: AsyncThrowingStream<SSERawEvent, Error>.Continuation

    init(continuation: AsyncThrowingStream<SSERawEvent, Error>.Continuation) {
        self.continuation = continuation
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            continuation.finish(throwing: RESTServiceError.requestFailed(
                statusCode: http.statusCode,
                code: "API_HTTP_\(http.statusCode)",
                message: "The assistant request failed."
            ))
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        // Split on newline bytes (0x0A), which cannot occur inside a multibyte UTF-8 sequence.
        while let idx = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer.subdata(in: buffer.startIndex..<idx)
            buffer.removeSubrange(buffer.startIndex...idx)
            let line = String(decoding: lineData, as: UTF8.self)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\r"))
            handle(line: line)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        // Flush remaining bytes and the final event block.
        if !buffer.isEmpty {
            let line = String(decoding: buffer, as: UTF8.self)
            buffer.removeAll()
            handle(line: line)
        }
        flushEvent()

        if let error, (error as NSError).code != NSURLErrorCancelled {
            continuation.finish(throwing: RESTServiceError.connectionFailed)
        } else {
            continuation.finish()
        }
    }

    private func handle(line: String) {
        if line.isEmpty {
            flushEvent()
            return
        }
        if line.hasPrefix("event:") {
            eventName = String(line.dropFirst("event:".count)).trimmingCharacters(in: .whitespaces)
        } else if line.hasPrefix("data:") {
            dataLines.append(String(line.dropFirst("data:".count)).trimmingCharacters(in: .whitespaces))
        }
    }

    private func flushEvent() {
        guard !dataLines.isEmpty || !eventName.isEmpty else { return }
        continuation.yield(SSERawEvent(event: eventName, data: dataLines.joined(separator: "\n")))
        eventName = ""
        dataLines.removeAll()
    }
}

extension Product {
    init(payload: ProductPayload) {
        let evidence = payload.evidence ?? []
        let detailEvidence = evidence.first { $0.sourceType == .productDetail }
        let reviews = ProductReview.reviews(payloads: payload.reviews, fallbackEvidence: evidence)
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
            reason: payload.summary ?? detailEvidence?.snippet ?? "",
            details: detailEvidence?.snippet ?? payload.summary ?? "",
            tags: payload.tags,
            specifications: specifications,
            imageURL: payload.imageURL,
            skus: skus,
            reviews: reviews
        )
        self.faqs = payload.faqs
        self.referenceOnly = payload.priceType == "reference"
        self.rating = payload.catalogSource?.averageRating
        self.ratingCount = payload.catalogSource?.ratingNumber
    }
}

private extension ProductReview {
    static func reviews(payloads: [ReviewPayload]?, fallbackEvidence: [EvidencePayload]) -> [ProductReview] {
        if let payloads, !payloads.isEmpty {
            return payloads.map(ProductReview.init(payload:))
        }
        return fallbackEvidence
            .filter { $0.sourceType == .userReview }
            .map(ProductReview.init(evidence:))
    }

    init(payload: ReviewPayload) {
        self.init(
            id: payload.id,
            nickname: payload.nickname,
            rating: min(5, max(0, payload.rating)),
            content: payload.content,
            polarity: payload.polarity
        )
    }

    init(evidence: EvidencePayload) {
        let parts = evidence.title
            .split(separator: "·", maxSplits: 1)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        let nickname = parts.first?.replacingOccurrences(of: "用户", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
        let ratingText = parts.dropFirst().first ?? ""
        let rating = Int(String(ratingText.filter(\.isNumber))) ?? 0
        self.init(
            id: evidence.id,
            nickname: (nickname?.isEmpty == false ? nickname : "Anonymous customer") ?? "Anonymous customer",
            rating: min(5, max(0, rating)),
            content: evidence.snippet,
            polarity: nil
        )
    }
}

extension RESTProductService {
    /// 商品要点；后端写不出来或请求失败时返回空数组，页面退回显示商家原文。
    func fetchHighlights(productID: String) async -> [ProductHighlight] {
        var request = URLRequest(url: baseURL.appendingPathComponent("products/\(productID)/highlights"))
        request.timeoutInterval = 30
        guard
            let (data, response) = try? await session.data(for: request),
            let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
            let envelope = try? JSONDecoder().decode(ProductHighlightEnvelope.self, from: data)
        else {
            return []
        }
        return envelope.highlights
    }
}

extension RESTProductService {
    /// 最近的订单，连同每个商品的图片地址（订单表里不存图）。
    func fetchOrders(userID: String = UserIdentity.defaultUserID) async throws -> (orders: [OrderPayload], images: [String: URL]) {
        let url = baseURL.appendingPathComponent("orders").appendingPathComponent(userID)
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw RESTServiceError.invalidResponse
        }
        let orders = try JSONDecoder().decode(OrderListEnvelope.self, from: data).orders
        let ids = Array(Set(orders.flatMap { $0.lines.map(\.productID) }))
        guard !ids.isEmpty else { return (orders, [:]) }
        let products = (try? await fetchProducts(productIDs: ids)) ?? []
        let images = products.reduce(into: [String: URL]()) { result, payload in
            if let url = Product(payload: payload).imageURL { result[payload.productID] = url }
        }
        return (orders, images)
    }
}
