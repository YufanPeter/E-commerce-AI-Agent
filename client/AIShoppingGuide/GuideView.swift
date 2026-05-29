import SwiftUI

struct GuideView: View {
    @Binding var cartItems: [CartItem]
    @State private var messages: [ChatMessage] = [
        ChatMessage(sender: .ai, text: "你可以问")
    ]
    @State private var inputText = ""
    @State private var isComposerExpanded = false
    @State private var showHistory = false
    @State private var selectedProduct: Product?
    @FocusState private var isInputFocused: Bool

    private let productService = RESTProductService()
    private let examples = ["适合油皮的洗面奶", "200 元内蓝牙耳机", "轻量跑鞋", "不要含酒精的防晒"]
    private let histories = [
        HistoryItem(title: "无酒精防晒推荐", subtitle: "今天 21:42 · 3 个商品"),
        HistoryItem(title: "通勤蓝牙耳机对比", subtitle: "昨天 · 预算 200 元内"),
        HistoryItem(title: "春季轻量跑鞋", subtitle: "周一 · 跑步场景")
    ]

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottomLeading) {
                VStack(spacing: 0) {
                    header
                    chatList
                }

                composerStack
                    .padding(.horizontal, 16)
                    .padding(.bottom, AppTheme.guideComposerBottomPadding)
                    .zIndex(1)
            }
            .background(AppTheme.background)
            .sheet(isPresented: $showHistory) {
                HistorySheet(items: histories)
                    .presentationDetents([.medium])
                    .presentationDragIndicator(.visible)
            }
            .navigationDestination(item: $selectedProduct) { product in
                ProductDetailView(product: product) { product, selectedOptions, quantity in
                    addToCart(product, selectedOptions: selectedOptions, quantity: quantity)
                }
            }
        }
    }

    private var header: some View {
        HStack {
            Text("CartPilot AI 导购助手")
                .font(.title3.bold())
                .foregroundStyle(AppTheme.textPrimary)
            Spacer()
            Button {
                showHistory = true
            } label: {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(AppTheme.primary)
                    .frame(width: 40, height: 40)
                    .floatingLiquidPanel(cornerRadius: 20)
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .padding(.bottom, 8)
    }

    private var chatList: some View {
        ScrollViewReader { proxy in
            ScrollView(showsIndicators: false) {
                LazyVStack(alignment: .leading, spacing: 16) {
                    ForEach(messages) { message in
                        MessageRow(
                            message: message,
                            examples: message.id == messages.first?.id ? examples : [],
                            onExampleTap: send,
                            onRetry: retryLast,
                            onProductTap: { selectedProduct = $0 }
                        )
                        .id(message.id)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.top, 12)
                .padding(.bottom, AppTheme.guideComposerBottomPadding + 112)
            }
            .scrollDismissesKeyboard(.interactively)
            .simultaneousGesture(
                TapGesture().onEnded {
                    isInputFocused = false
                }
            )
            .onChange(of: messages.count) { _, _ in
                if let last = messages.last?.id {
                    withAnimation(.easeOut(duration: 0.25)) {
                        proxy.scrollTo(last, anchor: .bottom)
                    }
                }
            }
        }
    }

    private var composerStack: some View {
        VStack(alignment: .leading, spacing: 10) {
            if isComposerExpanded {
                VStack(alignment: .leading, spacing: 12) {
                    ComposerAction(icon: "camera", title: "相机")
                    ComposerAction(icon: "photo", title: "图片上传")
                }
                .padding(14)
                .frame(width: 156, alignment: .leading)
                .floatingLiquidPanel(cornerRadius: 22)
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            HStack(spacing: 10) {
                Button {
                    withAnimation(.spring(response: 0.32, dampingFraction: 0.82)) {
                        isComposerExpanded.toggle()
                    }
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 18, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 36, height: 36)
                        .background(AppTheme.primary, in: Circle())
                }

                TextField("Ask a shopping question", text: $inputText)
                    .lineLimit(1)
                    .submitLabel(.send)
                    .onSubmit(sendCurrentInput)
                    .font(.subheadline)
                    .focused($isInputFocused)

                Button {} label: {
                    Image(systemName: "mic")
                        .foregroundStyle(AppTheme.textSecondary)
                }
                .buttonStyle(.plain)

                Button(action: sendCurrentInput) {
                    Image(systemName: "paperplane.fill")
                        .foregroundStyle(.white)
                        .frame(width: 34, height: 34)
                        .background(AppTheme.primary, in: Circle())
                }
                .buttonStyle(.plain)
            }
            .padding(10)
            .floatingLiquidPanel(cornerRadius: 28)
        }
    }

    private func sendCurrentInput() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        inputText = ""
        isInputFocused = false
        send(trimmed)
    }

    private func send(_ query: String) {
        isComposerExpanded = false
        messages.append(ChatMessage(sender: .user, text: query))
        messages.append(ChatMessage(sender: .ai, text: "正在理解你的需求", state: .understanding))
        simulateResponse(for: query)
    }

    private func retryLast() {
        messages.removeAll { $0.canRetry }
        simulateResponse(for: "重新推荐")
    }

    private func simulateResponse(for query: String) {
        Task {
            do {
                if query.contains("失败") || query.lowercased().contains("error") {
                    try await Task.sleep(nanoseconds: 550_000_000)
                    updateLastAI(text: "正在检索商品库与评价摘要", state: .retrieving)
                    try await Task.sleep(nanoseconds: 550_000_000)
                    updateLastAI(
                        text: "网络异常，暂时无法生成推荐，请稍后重试。",
                        state: .failed,
                        canRetry: true
                    )
                    return
                }

                let productIDs = productIDs(for: query)
                try await Task.sleep(nanoseconds: 550_000_000)
                updateLastAI(text: "正在按商品 ID 查询商品库", state: .retrieving)

                let payloads = try await productService.fetchProducts(productIDs: productIDs)
                let products = payloads.map(Product.init(payload:))

                try await Task.sleep(nanoseconds: 550_000_000)
                updateLastAI(text: "正在生成个性化推荐", state: .generating)

                try await Task.sleep(nanoseconds: 550_000_000)
                updateLastAI(
                    text: "我按商品 ID 从商品库取到了确定数据，并补上了价格、规格和图片。",
                    state: .ready,
                    products: products
                )
            } catch {
                updateLastAI(
                    text: errorMessage(error),
                    state: .failed,
                    canRetry: true
                )
            }
        }
    }

    private func productIDs(for query: String) -> [String] {
        let explicitIDs = extractProductIDs(from: query)
        if !explicitIDs.isEmpty {
            return explicitIDs
        }

        let lower = query.lowercased()
        if query.contains("耳机") || lower.contains("bluetooth") {
            return ["p_digital_007", "p_digital_018"]
        } else if query.contains("跑鞋") {
            return ["p_clothes_007", "p_clothes_009", "p_clothes_010"]
        } else if query.contains("防晒") || query.contains("酒精") {
            return ["p_beauty_023", "p_beauty_010", "p_beauty_006"]
        } else if query.contains("洗面奶") || query.contains("洁面") || query.contains("油皮") {
            return ["p_beauty_011"]
        }
        return ["p_beauty_008", "p_digital_007", "p_clothes_007"]
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

    private func errorMessage(_ error: Error) -> String {
        if let restError = error as? RESTServiceError {
            return restError.displayMessage
        }
        return "商品服务暂时不可用，请稍后重试。\n错误码：API_UNKNOWN_ERROR"
    }

    private func updateLastAI(
        text: String,
        state: MessageState,
        products: [Product] = [],
        canRetry: Bool = false
    ) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].text = text
        messages[index].state = state
        messages[index].products = products
        messages[index].canRetry = canRetry
    }

    private func addToCart(
        _ product: Product,
        selectedOptions: [String: String] = [:],
        quantity: Int = 1
    ) {
        let item = CartItem(product: product, selectedOptions: selectedOptions, quantity: quantity)
        if let index = cartItems.firstIndex(where: { $0.id == item.id }) {
            cartItems[index].quantity += quantity
        } else {
            cartItems.append(item)
        }
    }
}

struct MessageRow: View {
    let message: ChatMessage
    let examples: [String]
    let onExampleTap: (String) -> Void
    let onRetry: () -> Void
    let onProductTap: (Product) -> Void

    var body: some View {
        HStack(alignment: .top) {
            if message.sender == .user { Spacer(minLength: 44) }

            VStack(alignment: message.sender == .user ? .trailing : .leading, spacing: 10) {
                Text(displayText)
                    .font(.subheadline)
                    .foregroundStyle(message.state == .failed ? AppTheme.error : AppTheme.textPrimary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 11)
                    .background(bubbleColor, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .stroke(AppTheme.border, lineWidth: message.sender == .ai ? 1 : 0)
                    )

                if !examples.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(examples, id: \.self) { example in
                            Button(example) { onExampleTap(example) }
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(AppTheme.primary)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 10)
                                .frame(maxWidth: 220, alignment: .leading)
                                .background(AppTheme.softPurple, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        }
                    }
                }

                if message.canRetry {
                    Button("重试", action: onRetry)
                        .buttonStyle(.borderedProminent)
                        .tint(AppTheme.primary)
                }

                ForEach(message.products) { product in
                    ProductCard(product: product) {
                        onProductTap(product)
                    }
                }
            }

            if message.sender == .ai { Spacer(minLength: 44) }
        }
    }

    private var displayText: String {
        switch message.state {
        case .ready, .failed:
            return message.text
        default:
            return "\(message.state.rawValue)：\(message.text)"
        }
    }

    private var bubbleColor: Color {
        switch message.sender {
        case .user:
            return AppTheme.softBlue
        case .ai:
            return message.state == .failed ? AppTheme.error.opacity(0.08) : AppTheme.surface
        }
    }
}

struct ComposerAction: View {
    let icon: String
    let title: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .foregroundStyle(AppTheme.primary)
                .frame(width: 26, height: 26)
                .background(AppTheme.primary.opacity(0.10), in: Circle())
            Text(title)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(AppTheme.textPrimary)
        }
    }
}

struct ProductCard: View {
    let product: Product
    let onDetail: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ProductRemoteImage(url: product.imageURL, cornerRadius: 16, placeholderIcon: "shippingbox", contentMode: .fit)
                .frame(maxWidth: .infinity)
                .frame(maxHeight: 300)

            if !product.tags.isEmpty {
                HStack(spacing: 8) {
                    ForEach(product.tags, id: \.self) { tag in
                        Text(tag)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(AppTheme.primary)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 7)
                            .background(AppTheme.softPurple, in: Capsule())
                    }
                }
            }

            Text(product.title)
                .font(.headline)
                .foregroundStyle(AppTheme.textPrimary)
            Text(product.priceDisplay(for: product.defaultSpecificationSelection))
                .font(.title3.bold())
                .foregroundStyle(AppTheme.error)
//            if !product.reason.isEmpty {
//                Text(product.reason)
//                    .font(.subheadline)
//                    .foregroundStyle(AppTheme.textSecondary)
//                    .lineLimit(3)
//            }

            Button(action: onDetail) {
                Text("查看详情")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 11)
                    .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
        }
        .padding(14)
        .floatingLiquidPanel(cornerRadius: 22)
    }
}

struct ProductRemoteImage: View {
    let url: URL?
    let cornerRadius: CGFloat
    let placeholderIcon: String
    var contentMode: ContentMode = .fill

    var body: some View {
        AsyncImage(url: url, transaction: Transaction(animation: .easeOut(duration: 0.3))) { phase in
            switch phase {
            case .success(let image):
                if contentMode == .fit {
                    image
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity)
                        .background(Color.white)
                        .transition(.opacity)
                } else {
                    image
                        .resizable()
                        .scaledToFill()
                        .transition(.opacity)
                }
            case .empty:
                placeholderBox { ProgressView().tint(AppTheme.primary) }
            case .failure:
                placeholderBox { placeholderIconView }
            @unknown default:
                placeholderBox { placeholderIconView }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
    }

    @ViewBuilder
    private func placeholderBox<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        ZStack {
            background
            content()
        }
        .modifier(PlaceholderSizing(isFit: contentMode == .fit))
    }

    @ViewBuilder
    private var background: some View {
        if contentMode == .fit {
            Color.white
        } else {
            LinearGradient(colors: [AppTheme.softPurple, AppTheme.softBlue], startPoint: .topLeading, endPoint: .bottomTrailing)
        }
    }

    private var placeholderIconView: some View {
        Image(systemName: placeholderIcon)
            .font(.system(size: 34, weight: .semibold))
            .foregroundStyle(AppTheme.primary)
    }
}

private struct PlaceholderSizing: ViewModifier {
    let isFit: Bool

    func body(content: Content) -> some View {
        if isFit {
            content
                .aspectRatio(1, contentMode: .fit)
                .frame(maxWidth: .infinity)
        } else {
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

struct HistorySheet: View {
    let items: [HistoryItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("历史记录")
                .font(.title3.bold())
                .padding(.top, 10)

            ForEach(items) { item in
                HStack(spacing: 12) {
                    Image(systemName: "message")
                        .foregroundStyle(AppTheme.primary)
                        .frame(width: 40, height: 40)
                        .background(AppTheme.softPurple, in: Circle())
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.title).font(.headline)
                        Text(item.subtitle).font(.caption).foregroundStyle(AppTheme.textSecondary)
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(AppTheme.textSecondary)
                }
                .padding(12)
                .floatingLiquidPanel(cornerRadius: 18)
            }
            Spacer()
        }
        .padding(20)
        .background(AppTheme.background)
    }
}
