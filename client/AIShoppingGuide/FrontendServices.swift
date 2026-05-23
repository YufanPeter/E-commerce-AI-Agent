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
