import Foundation
import SwiftUI

/// 后端连接配置（集中管理，便于真机调试时切换到局域网 IP）。
///
/// - iOS 模拟器：保持默认 `http://127.0.0.1:8000`（与 Mac 共享 localhost）。
/// - iOS 真机：把后端用 `HOST=0.0.0.0 ./scripts/start_backend.sh` 启动，
///   再把下面的 `defaultBaseURL` 改成 Mac 的局域网地址，例如
///   `http://192.168.1.23:8000`（脚本就绪后会在终端打印该地址）。
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

    /// 健康检查端点。
    static var healthURL: URL { defaultBaseURL.appendingPathComponent("health") }

    /// 购物车清空端点：App 冷启动调用，清掉后端跨重启残留的购物车。
    static var cartResetURL: URL { defaultBaseURL.appendingPathComponent("cart/reset") }
}

/// 后端可达性状态。
enum BackendReachability: Equatable {
    case unknown
    case checking
    case reachable
    case unreachable
}

/// 启动时及后续轮询后端 `/health`，向 UI 暴露可达性状态，
/// 让 App 在后端未启动时给出明确引导，而不是等用户发消息才报错。
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

    /// 主动检查一次后端是否就绪。
    @discardableResult
    func check() async -> BackendReachability {
        status = .checking
        let result = await probe()
        status = result
        return result
    }

    /// 开始周期性轮询；后端不可达时每 3 秒重试，可达后降到每 15 秒做保活探测。
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

/// 跨轮复用 session_id 的线程安全存储。
private actor SessionIDStore {
    private var sessionID: String?
    func current() -> String? { sessionID }
    func update(_ value: String) { sessionID = value }
}

/// 真实 Agent 服务：连接后端 `/chat/stream`（SSE），驱动 LLM + RAG 流水线。
///
/// 后端事件 → 客户端事件映射：
///   - `session`     → 记录 session_id（多轮上下文复用，不向 UI 透出）
///   - `status`      → 阶段提示（routing→understanding / tool→retrieving / compose→generating）
///   - `tool_result` → 取出商品 ID，调 `/products` 补全完整数据后以 `.products` 透出
///   - `token`       → 文本增量（拼接为最终回答）
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

    /// 流式请求超时放宽：首轮会触发后端加载 embedding 模型，冷启动可能 ~60s。
    private static func makeStreamingConfig() -> URLSessionConfiguration {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 300
        config.waitsForConnectivity = true
        // SSE：禁用缓存，避免响应被本地缓存层攒着不实时下发。
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
        var body: [String: Any] = ["query": payload.text]
        if let resolvedSession { body["session_id"] = resolvedSession }
        urlRequest.httpBody = try JSONSerialization.data(withJSONObject: body)

        // 用 URLSessionDataDelegate 做增量 SSE 解析：URLSession.AsyncBytes 在 iOS 上
        // 会把流式响应攒在缓冲里不实时下发，导致 UI 卡在中间状态。delegate 的
        // didReceive(data:) 是每个网络 chunk 立即回调，最可靠。
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
            // 多规格加购：后端要求用户选规格 → 透出可交互的规格选择卡片，
            // 不再继续解析商品/购物车（此时尚未真正加购）。
            if let spec = Self.extractSpecSelection(fromToolResult: bytes) {
                continuation.yield(
                    AgentStreamEventPayload(type: .specSelection, specSelection: spec)
                )
                return
            }
            if Self.isCartToolResult(bytes) {
                // 必须同步 await：购物车快照要在 done 之前按顺序透出，否则
                // 加购很快返回时（needs_composer=False）detached Task 还没取完
                // 商品就被 done 收尾，导致购物车显示为空 / 价格错位。
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
            // 补全：Agent 只给精简卡片，这里用商品 ID 取完整数据用于渲染。
            if let payloads = try? await productService.fetchProducts(productIDs: ids), !payloads.isEmpty {
                continuation.yield(AgentStreamEventPayload(type: .products, products: payloads))
            }

        case "token":
            // data 是 JSON 编码的字符串，例如 data: "一段文本"
            if let piece = try? JSONDecoder().decode(String.self, from: bytes), !piece.isEmpty {
                continuation.yield(AgentStreamEventPayload(type: .textDelta, textDelta: piece))
            } else if !data.isEmpty {
                continuation.yield(AgentStreamEventPayload(type: .textDelta, textDelta: data))
            }

        case "done":
            continuation.yield(AgentStreamEventPayload(type: .done))

        case "error":
            let message = (try? JSONSerialization.jsonObject(with: bytes) as? [String: Any])?["message"] as? String
            throw RESTServiceError.requestFailed(
                statusCode: 500,
                code: "AGENT_STREAM_ERROR",
                message: message ?? "Agent 流式处理失败。"
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

    /// 从 cart 工具的 `ask_spec` 结果中解析规格选择卡片所需数据。
    /// 仅当 action==ask_spec 且带有有序的 `dimensions` 时返回，否则 nil。
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

/// 一条已切分好的原始 SSE 事件（event 名 + 合并后的 data 文本）。
private struct SSERawEvent {
    let event: String
    let data: String
}

/// 基于 `URLSessionDataDelegate` 的 SSE 增量解析器。
///
/// 每收到一个网络 chunk（`didReceive data:`）就立即按字节查找 `\n`，切出完整行后
/// 解析 `event:` / `data:`，遇到空行即把累积的事件 yield 给上层 `AsyncThrowingStream`。
/// 这样能保证 token/done 等后续事件实时到达，避免 `URLSession.AsyncBytes` 的缓冲问题。
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
                message: "Agent 服务请求失败。"
            ))
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        // 按 \n(0x0A) 逐行切分；\n 不会出现在 UTF-8 多字节序列中间，按字节切分是安全的。
        while let idx = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer.subdata(in: buffer.startIndex..<idx)
            buffer.removeSubrange(buffer.startIndex...idx)
            let line = String(decoding: lineData, as: UTF8.self)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\r"))
            handle(line: line)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        // flush 残留字节与最后一个事件块
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
