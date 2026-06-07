import Foundation

enum MockServiceError: Error {
    case notFound
    case invalidRequest
}

final class MockAgentService: AgentServicing {
    func streamResponse(
        for request: AgentRequestPayload
    ) -> AsyncThrowingStream<AgentStreamEventPayload, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                continuation.yield(
                    AgentStreamEventPayload(
                        type: .status,
                        status: AgentStatusPayload(
                            phase: .understanding,
                            message: "正在理解你的需求"
                        )
                    )
                )
                try? await Task.sleep(nanoseconds: 500_000_000)

                guard !Task.isCancelled else {
                    continuation.finish()
                    return
                }

                continuation.yield(
                    AgentStreamEventPayload(
                        type: .status,
                        status: AgentStatusPayload(
                            phase: .retrieving,
                            message: "正在检索商品"
                        )
                    )
                )
                try? await Task.sleep(nanoseconds: 500_000_000)

                guard !Task.isCancelled else {
                    continuation.finish()
                    return
                }

                continuation.yield(
                    AgentStreamEventPayload(
                        type: .status,
                        status: AgentStatusPayload(
                            phase: .generating,
                            message: "正在生成推荐"
                        )
                    )
                )
                try? await Task.sleep(nanoseconds: 300_000_000)

                // 返回商品
                let products = MockProductCatalog.products(matching: request.text)
                continuation.yield(
                    AgentStreamEventPayload(
                        type: .products,
                        products: products
                    )
                )
                try? await Task.sleep(nanoseconds: 200_000_000)

                // 返回文本（包含开场白、解说、追问）
                let responseText = generateMockResponseText(query: request.text, products: products)
                for (index, char) in responseText.enumerated() {
                    continuation.yield(
                        AgentStreamEventPayload(
                            type: .textDelta,
                            textDelta: String(char)
                        )
                    )
                    try? await Task.sleep(nanoseconds: 30_000_000)
                }

                continuation.yield(
                    AgentStreamEventPayload(
                        type: .status,
                        status: AgentStatusPayload(phase: .done, message: "Done")
                    )
                )
                continuation.yield(AgentStreamEventPayload(type: .done))
                continuation.finish()
            }

            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    func cancel(sessionID: String) async {}
}

private func generateMockResponseText(query: String, products: [ProductPayload]) -> String {
    let productCount = min(products.count, 3)
    
    // 开场白
    let opening: String
    if query.lowercased().contains("平板") || query.lowercased().contains("电脑") {
        opening = "为你推荐了 \(productCount) 款高性能平板电脑"
    } else if query.lowercased().contains("手机") {
        opening = "为你推荐了 \(productCount) 款热门智能手机"
    } else if query.lowercased().contains("耳机") || query.lowercased().contains("音箱") {
        opening = "为你推荐了 \(productCount) 款优质音频设备"
    } else {
        opening = "为你推荐了 \(productCount) 款精选商品"
    }

    // 商品解说（每个商品一段）
    var descriptions: [String] = []
    for (index, product) in products.prefix(productCount).enumerated() {
        let features = [
            "性能强劲，流畅运行各种应用",
            "续航持久，满足全天使用需求",
            "屏幕出色，视觉体验极佳",
            "设计精美，手感舒适",
            "性价比高，物超所值"
        ]
        let feature = features[index % features.count]
        let description = "\(product.title)：\(feature)"
        descriptions.append(description)
    }

    // 追问方向
    let questions = [
        "需要更平价的选择？",
        "想要看特定品牌？",
        "需要详细对比某两款？",
        "想了解更多规格细节？"
    ]

    // 组合成完整文本
    var text = opening + "\n\n"
    text += descriptions.joined(separator: "\n\n") + "\n\n"
    text += questions.joined(separator: "\n")

    return text
}

final class MockProductService: ProductServicing {
    func fetchProduct(productID: String) async throws -> ProductPayload {
        guard let product = MockProductCatalog.all.first(where: { $0.productID == productID }) else {
            throw MockServiceError.notFound
        }
        return product
    }

    func searchProducts(_ request: ProductSearchRequest) async throws -> ProductSearchResponse {
        let products = MockProductCatalog.products(matching: request.query)
            .prefix(request.limit)
        return ProductSearchResponse(
            requestID: UUID().uuidString,
            products: Array(products),
            evidence: products.flatMap { $0.evidence ?? [] }
        )
    }

    func compareProducts(_ request: ProductComparisonRequest) async throws -> ProductComparisonPayload {
        let products = MockProductCatalog.all.filter { request.productIDs.contains($0.productID) }
        guard !products.isEmpty else {
            throw MockServiceError.notFound
        }

        let headers = products.map { product in
            ComparisonProductPayload(
                productID: product.productID,
                title: product.title,
                brand: product.brand,
                price: Double(product.price.amountMinor) / 100,
                imageURL: product.imageURL
            )
        }
        let priceRow = ComparisonRowPayload(
            label: "价格",
            values: products.map { $0.price.display },
            highlight: 0
        )
        let reasonRow = ComparisonRowPayload(
            label: "推荐点",
            values: products.map { $0.summary ?? "" },
            highlight: nil
        )

        return ProductComparisonPayload(
            title: "商品对比",
            products: headers,
            rows: [priceRow, reasonRow],
            recommendation: products.first?.title ?? ""
        )
    }
}

final class MockCartService: CartServicing {
    private var snapshot: CartSnapshotPayload = MockCartFactory.initialSnapshot()

    func fetchCart() async throws -> CartSnapshotPayload {
        snapshot
    }

    func mutateCart(_ request: CartMutationRequest) async throws -> CartSnapshotPayload {
        switch request.action {
        case .add:
            guard let productID = request.productID,
                  let product = MockProductCatalog.all.first(where: { $0.productID == productID })
            else {
                throw MockServiceError.invalidRequest
            }
            add(product: product, request: request)

        case .updateQuantity:
            guard let cartItemID = request.cartItemID, let quantity = request.quantity else {
                throw MockServiceError.invalidRequest
            }
            updateItem(cartItemID: cartItemID) { item in
                CartItemPayload(
                    id: item.id,
                    productID: item.productID,
                    skuID: item.skuID,
                    product: item.product,
                    selectedOptions: item.selectedOptions,
                    quantity: max(1, quantity),
                    isSelected: item.isSelected,
                    lineTotal: MockCartFactory.lineTotal(for: item.product, quantity: max(1, quantity))
                )
            }

        case .remove:
            guard let cartItemID = request.cartItemID else {
                throw MockServiceError.invalidRequest
            }
            replaceItems(snapshot.items.filter { $0.id != cartItemID })

        case .select:
            replaceSelection(ids: request.selectedCartItemIDs, selected: true)

        case .deselect:
            replaceSelection(ids: request.selectedCartItemIDs, selected: false)

        case .selectAll:
            replaceItems(snapshot.items.map { item in
                updateSelection(item: item, isSelected: true)
            })

        case .clearSelection:
            replaceItems(snapshot.items.map { item in
                updateSelection(item: item, isSelected: false)
            })

        case .updateSpecification:
            guard let cartItemID = request.cartItemID else {
                throw MockServiceError.invalidRequest
            }
            updateItem(cartItemID: cartItemID) { item in
                CartItemPayload(
                    id: item.id,
                    productID: item.productID,
                    skuID: request.skuID ?? item.skuID,
                    product: item.product,
                    selectedOptions: request.selectedOptions,
                    quantity: item.quantity,
                    isSelected: item.isSelected,
                    lineTotal: item.lineTotal
                )
            }
        }

        return snapshot
    }

    func createOrderPreview(selectedCartItemIDs: [String]) async throws -> OrderPreviewPayload {
        let selectedItems = snapshot.items.filter { selectedCartItemIDs.contains($0.id) }
        return OrderPreviewPayload(
            orderID: UUID().uuidString,
            items: selectedItems.map { item in
                OrderLineItemPayload(
                    id: item.id,
                    product: item.product,
                    selectedOptions: item.selectedOptions,
                    quantity: item.quantity,
                    lineTotal: item.lineTotal
                )
            },
            address: MockUserProfileService.defaultAddress,
            priceSummary: MockCartFactory.priceSummary(for: selectedItems),
            warnings: selectedItems.isEmpty ? ["No selected cart items"] : []
        )
    }

    func confirmOrder(_ request: OrderConfirmRequest) async throws -> OrderConfirmationPayload {
        OrderConfirmationPayload(
            orderID: request.orderID,
            status: "confirmed",
            createdAt: Date()
        )
    }

    private func add(product: ProductPayload, request: CartMutationRequest) {
        let quantity = max(1, request.quantity ?? 1)
        let itemID = [product.productID, request.skuID].compactMap { $0 }.joined(separator: "|")
        if let index = snapshot.items.firstIndex(where: { $0.id == itemID }) {
            let current = snapshot.items[index]
            let updatedQuantity = current.quantity + quantity
            updateItem(cartItemID: itemID) { item in
                CartItemPayload(
                    id: item.id,
                    productID: item.productID,
                    skuID: item.skuID,
                    product: item.product,
                    selectedOptions: item.selectedOptions,
                    quantity: updatedQuantity,
                    isSelected: true,
                    lineTotal: MockCartFactory.lineTotal(for: item.product, quantity: updatedQuantity)
                )
            }
        } else {
            let item = CartItemPayload(
                id: itemID,
                productID: product.productID,
                skuID: request.skuID,
                product: product,
                selectedOptions: request.selectedOptions,
                quantity: quantity,
                isSelected: true,
                lineTotal: MockCartFactory.lineTotal(for: product, quantity: quantity)
            )
            replaceItems(snapshot.items + [item])
        }
    }

    private func updateItem(
        cartItemID: String,
        transform: (CartItemPayload) -> CartItemPayload
    ) {
        replaceItems(
            snapshot.items.map { item in
                item.id == cartItemID ? transform(item) : item
            }
        )
    }

    private func replaceSelection(ids: [String], selected: Bool) {
        let idSet = Set(ids)
        replaceItems(snapshot.items.map { item in
            idSet.contains(item.id) ? updateSelection(item: item, isSelected: selected) : item
        })
    }

    private func updateSelection(item: CartItemPayload, isSelected: Bool) -> CartItemPayload {
        CartItemPayload(
            id: item.id,
            productID: item.productID,
            skuID: item.skuID,
            product: item.product,
            selectedOptions: item.selectedOptions,
            quantity: item.quantity,
            isSelected: isSelected,
            lineTotal: item.lineTotal
        )
    }

    private func replaceItems(_ items: [CartItemPayload]) {
        snapshot = CartSnapshotPayload(
            cartID: snapshot.cartID,
            items: items,
            selectedItemIDs: items.filter(\.isSelected).map(\.id),
            priceSummary: MockCartFactory.priceSummary(for: items.filter(\.isSelected)),
            updatedAt: Date()
        )
    }
}

final class MockUserProfileService: UserProfileServicing {
    static let defaultAddress = AddressPayload(
        id: "address_default",
        receiverName: "Demo User",
        phoneMasked: "138****0000",
        fullAddress: "Default demo address",
        isDefault: true
    )

    private var profile = UserProfilePayload(
        userID: "demo_user",
        budgetMin: nil,
        budgetMax: Money(currency: "CNY", amountMinor: 50000, display: "CNY 500"),
        preferredCategories: ["beauty", "digital"],
        excludedIngredients: ["alcohol"],
        excludedBrands: [],
        attributes: ["skinType": "oily"],
        defaultAddressID: "address_default"
    )

    func fetchProfile() async throws -> UserProfilePayload {
        profile
    }

    func updateProfile(_ request: UserProfileUpdateRequest) async throws -> UserProfilePayload {
        profile = UserProfilePayload(
            userID: profile.userID,
            budgetMin: request.budgetMin ?? profile.budgetMin,
            budgetMax: request.budgetMax ?? profile.budgetMax,
            preferredCategories: request.preferredCategories ?? profile.preferredCategories,
            excludedIngredients: request.excludedIngredients ?? profile.excludedIngredients,
            excludedBrands: request.excludedBrands ?? profile.excludedBrands,
            attributes: request.attributes ?? profile.attributes,
            defaultAddressID: request.defaultAddressID ?? profile.defaultAddressID
        )
        return profile
    }

    func fetchAddresses() async throws -> [AddressPayload] {
        [Self.defaultAddress]
    }
}

final class MockMediaUploadService: MediaUploadServicing {
    func createUploadTicket(
        intent: UploadIntent,
        mimeType: String,
        sizeBytes: Int
    ) async throws -> UploadTicketPayload {
        UploadTicketPayload(
            uploadID: UUID().uuidString,
            uploadURL: URL(string: "https://example.invalid/upload")!,
            expiresAt: Date().addingTimeInterval(600),
            headers: ["Content-Type": mimeType]
        )
    }

    func uploadAttachment(
        localURL: URL,
        ticket: UploadTicketPayload,
        kind: AttachmentKind,
        mimeType: String
    ) async throws -> AttachmentPayload {
        AttachmentPayload(
            id: ticket.uploadID,
            kind: kind,
            mimeType: mimeType,
            localURL: localURL,
            remoteURL: URL(string: "https://example.invalid/files/\(ticket.uploadID)")!,
            sizeBytes: nil,
            metadata: [:]
        )
    }
}

final class MockAnalyticsService: AnalyticsServicing {
    private(set) var events: [AnalyticsEventPayload] = []

    func track(_ event: AnalyticsEventPayload) async {
        events.append(event)
    }
}

private enum MockProductCatalog {
    static let all: [ProductPayload] = Product.samples.enumerated().map { index, product in
        makePayload(from: product, index: index)
    }

    static func products(matching query: String) -> [ProductPayload] {
        let lowercased = query.lowercased()
        let filtered = all.filter { product in
            product.title.lowercased().contains(lowercased)
            || (product.summary ?? "").lowercased().contains(lowercased)
            || product.tags.contains { tag in query.contains(tag) || lowercased.contains(tag.lowercased()) }
        }
        return filtered.isEmpty ? all : filtered
    }

    private static func makePayload(from product: Product, index: Int) -> ProductPayload {
        let productID = "mock_product_\(index + 1)"
        let price = money(from: product.price)
        let specs = product.specifications.map { spec in
            ProductSpecificationPayload(
                id: spec.name,
                name: spec.name,
                options: spec.options.map { option in
                    ProductSpecOptionPayload(id: "\(spec.name)_\(option)", label: option, isAvailable: true)
                }
            )
        }
        return ProductPayload(
            productID: productID,
            title: product.title,
            category: product.tags.first ?? "general",
            brand: nil,
            imageURL: nil,
            detailURL: nil,
            price: price,
            originalPrice: nil,
            availability: .inStock,
            summary: product.reason,
            tags: product.tags,
            specifications: specs,
            skus: [
                SKUPayload(
                    id: "\(productID)_default",
                    selectedOptions: product.defaultSpecificationSelection,
                    price: price,
                    availability: .inStock,
                    stockCount: nil
                )
            ],
            evidence: [
                EvidencePayload(
                    id: "\(productID)_detail",
                    sourceType: .productDetail,
                    title: product.title,
                    snippet: product.details,
                    score: 1,
                    updatedAt: Date()
                )
            ],
            updatedAt: Date()
        )
    }

    static func money(from display: String) -> Money {
        let cleaned = display
            .replacingOccurrences(of: "¥", with: "")
            .replacingOccurrences(of: ",", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let amount = Double(cleaned) ?? 0
        return Money(
            currency: "CNY",
            amountMinor: Int((amount * 100).rounded()),
            display: display
        )
    }
}

private enum MockCartFactory {
    static func initialSnapshot() -> CartSnapshotPayload {
        let items = MockProductCatalog.all.prefix(2).map { product in
            let quantity = 1
            return CartItemPayload(
                id: product.productID,
                productID: product.productID,
                skuID: product.skus.first?.id,
                product: product,
                selectedOptions: product.skus.first?.selectedOptions ?? [:],
                quantity: quantity,
                isSelected: true,
                lineTotal: lineTotal(for: product, quantity: quantity)
            )
        }
        return CartSnapshotPayload(
            cartID: "mock_cart",
            items: Array(items),
            selectedItemIDs: items.map(\.id),
            priceSummary: priceSummary(for: Array(items)),
            updatedAt: Date()
        )
    }

    static func lineTotal(for product: ProductPayload, quantity: Int) -> Money {
        let amountMinor = product.price.amountMinor * quantity
        return Money(
            currency: product.price.currency,
            amountMinor: amountMinor,
            display: "\(product.price.currency) \(Double(amountMinor) / 100)"
        )
    }

    static func priceSummary(for items: [CartItemPayload]) -> CartPriceSummaryPayload {
        let amountMinor = items.reduce(0) { $0 + $1.lineTotal.amountMinor }
        let subtotal = Money(
            currency: "CNY",
            amountMinor: amountMinor,
            display: "CNY \(Double(amountMinor) / 100)"
        )
        return CartPriceSummaryPayload(
            subtotal: subtotal,
            discount: nil,
            payable: subtotal
        )
    }
}
