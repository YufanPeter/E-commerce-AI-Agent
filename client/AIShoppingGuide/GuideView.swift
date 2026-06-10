import AVFoundation
import Combine
import PhotosUI
import Speech
import SwiftUI
import UIKit

private struct ComposerHeightPreferenceKey: PreferenceKey {
    static var defaultValue: CGFloat = 56

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}

/// 聊天滚动几何快照：是否接近底部 + 内容是否真正可滚动（超过一屏）。
private struct ScrollSnapshot: Equatable {
    let isNearBottom: Bool
    let isScrollable: Bool
}

/// 进入对比页的上下文：携带候选商品（当前这条 AI 消息推荐的商品），
/// 对比页内用下拉菜单从中挑选 2-3 件进行对比。
struct ComparisonContext: Identifiable, Hashable {
    let id = UUID()
    let candidates: [Product]
}

struct GuideView: View {
    @Binding var cartItems: [CartItem]
    @StateObject private var store = ConversationStore()
    @State private var messages: [ChatMessage] = []
    @State private var inputText = ""
    @State private var isComposerExpanded = false
    @State private var showHistory = false
    @State private var productDetailContext: ProductDetailContext?
    @State private var comparisonContext: ComparisonContext?
    @State private var lastQuery: String = ""
    @State private var currentConversationID = UUID()
    @State private var currentSessionID = UUID().uuidString
    @State private var currentTitle = GuideView.newConversationTitle
    @State private var examples: [String] = GuideView.freshExamples()
    @State private var suggestions: HomeSuggestions?
    @State private var isRefreshingHot = false
    @State private var keyboardHeight: CGFloat = 0
    @State private var composerHeight: CGFloat = 56
    @State private var isAutoFollowEnabled = true
    @State private var isNearChatBottom = true
    @State private var isUserInteractingWithChat = false
    @State private var shouldShowJumpToLatest = false
    /// 内容是否超过视口（真正可滚动）。只有可滚动且不在底部时才显示"跳到最新"。
    @State private var isChatScrollable = false
    @State private var pendingQuestionAnchorID: UUID?
    @State private var shouldHoldLatestQuestionAnchor = false
    @State private var pendingAutoFollowWorkItem: DispatchWorkItem?
    @State private var photoPickerItem: PhotosPickerItem?
    @State private var showPhotoPicker = false
    @State private var showCamera = false
    @State private var chatListIdentity = UUID()
    /// 待发送的图片草稿：选/拍图后先挂在输入区，待用户配上文字一起发送；nil 表示无附件。
    @State private var pendingImageData: Data?
    @StateObject private var speechInput = SpeechInputController()
    @FocusState private var isInputFocused: Bool

    private let agentService = RESTAgentService()
    private let productService = RESTProductService()
    private let nearBottomThreshold: CGFloat = 96
    private let questionAnchorCharacterLimit = 220
    private let autoFollowTimer = Timer.publish(every: 0.12, on: .main, in: .common).autoconnect()
    private let streamDefaultDelay: UInt64 = 16_000_000
    private let streamWhitespaceDelay: UInt64 = 6_000_000
    private let streamPunctuationDelay: UInt64 = 70_000_000
    private let streamLineBreakDelay: UInt64 = 110_000_000
    private let streamCardRevealDelay: UInt64 = 180_000_000

    /// 示例 query 池：每次空态出现时随机取 4 条，避免每次都是同样几个。
    private static let examplePool = [
        "适合油皮的洗面奶", "200 元内蓝牙耳机", "轻量跑鞋", "不要含酒精的防晒",
        "通勤双肩包推荐", "敏感肌身体乳", "适合送女友的香水", "300 元内机械键盘",
        "冬天保暖羽绒服", "学生党平价护眼台灯", "适合露营的折叠椅", "低糖代餐零食",
        "降噪头戴式耳机", "夏天透气运动短裤", "适合新手的口红色号", "家用空气炸锅"
    ]

    /// 从池子里随机抽 4 条不重复示例。
    private static func freshExamples() -> [String] {
        Array(examplePool.shuffled().prefix(4))
    }

    private static let newConversationTitle = "新对话"
    private static let emptyStateAnchorID = "guide-empty-state-anchor"

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottomLeading) {
                VStack(spacing: 0) {
                    header
                    chatList
                }

                composerStack
                    .padding(.horizontal, 16)
                    .background(
                        GeometryReader { geometry in
                            Color.clear.preference(key: ComposerHeightPreferenceKey.self, value: geometry.size.height)
                        }
                    )
                    .padding(.bottom, composerBottomPadding)
                    .zIndex(1)
            }
            .background(AppTheme.background)
            .onPreferenceChange(ComposerHeightPreferenceKey.self) { height in
                guard height > 0 else { return }
                guard abs(composerHeight - height) > 0.5 else { return }
                composerHeight = height
            }
            .sheet(isPresented: $showHistory) {
                HistorySheet(
                    conversations: store.conversations,
                    onSelect: { openConversation($0) },
                    onDelete: { store.delete($0) },
                    onRename: { conversation, newTitle in
                        store.rename(conversation, to: newTitle)
                        // 若改的是当前对话，同步标题，避免下次 persist 覆盖回去。
                        if conversation.id == currentConversationID {
                            currentTitle = newTitle
                        }
                    },
                    onClearAll: { store.clearAll() }
                )
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
            }
            .navigationDestination(item: $productDetailContext) { context in
                ProductDetailView(product: context.product) { product, selectedOptions, quantity in
                    addToCart(product, selectedOptions: selectedOptions, quantity: quantity)
                }
            }
            .navigationDestination(item: $comparisonContext) { context in
                ComparisonView(candidates: context.candidates)
            }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillChangeFrameNotification)) { notification in
                updateKeyboardHeight(from: notification)
            }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { notification in
                updateKeyboardHeight(from: notification)
            }
            .photosPicker(isPresented: $showPhotoPicker, selection: $photoPickerItem, matching: .images)
            .onChange(of: photoPickerItem) { _, newItem in
                guard let newItem else { return }
                Task { @MainActor in
                    if let data = try? await newItem.loadTransferable(type: Data.self) {
                        handlePickedImage(data)
                    }
                    photoPickerItem = nil
                }
            }
            .fullScreenCover(isPresented: $showCamera) {
                CameraPicker { image in
                    if let data = image.jpegData(compressionQuality: 1.0) {
                        handlePickedImage(data)
                    }
                }
                .ignoresSafeArea()
            }
            .task {
                if suggestions == nil {
                    await loadSuggestions()
                }
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("CartPilot")
                    .font(.system(size: 26, weight: .bold, design: .rounded))
                    .foregroundStyle(AppTheme.textPrimary)
                Text("购物导购")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.textSecondary)
            }
            Spacer()
            Button {
                startNewConversation()
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "square.and.pencil")
                        .font(.system(size: 15, weight: .semibold))
                    Text("新对话")
                        .font(.subheadline.weight(.semibold))
                }
                .foregroundStyle(AppTheme.primary)
                .padding(.horizontal, 14)
                .frame(height: 40)
                .surfacePanel(cornerRadius: 20)
            }
            .buttonStyle(.tactile)
            Button {
                showHistory = true
            } label: {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(AppTheme.primary)
                    .frame(width: 40, height: 40)
                    .surfacePanel(cornerRadius: 20)
            }
            .buttonStyle(.tactile)
        }
        .padding(.horizontal, 20)
        .padding(.top, 18)
        .padding(.bottom, 8)
    }

    private var chatList: some View {
        ScrollViewReader { proxy in
            ZStack(alignment: .bottomTrailing) {
                trackedChatScrollView
                    .id(chatListIdentity)
                    .onChange(of: currentConversationID) { _, _ in
                        scrollToEmptyState(proxy)
                    }
                    .onChange(of: scrollAnchorToken) { _, _ in
                        driveAutoScroll(proxy)
                    }

                if shouldShowJumpToLatest && isChatScrollable {
                    jumpToLatestButton {
                        jumpToLatest(proxy)
                    }
                    .padding(.trailing, 18)
                    .padding(.bottom, jumpButtonBottomPadding)
                    .transition(.scale(scale: 0.92).combined(with: .opacity))
                    .zIndex(2)
                }
            }
        }
    }

    @ViewBuilder
    private var trackedChatScrollView: some View {
        if #available(iOS 18.0, *) {
            chatScrollView
                .onScrollGeometryChange(for: ScrollSnapshot.self) { geometry in
                    let distanceFromBottom = geometry.contentSize.height - geometry.visibleRect.maxY
                    // 内容高度比可视区高出一屏阈值以上，才算"真正可滚动"。
                    let scrollable = geometry.contentSize.height > geometry.containerSize.height + nearBottomThreshold
                    return ScrollSnapshot(
                        isNearBottom: distanceFromBottom <= nearBottomThreshold,
                        isScrollable: scrollable
                    )
                } action: { _, snapshot in
                    if isChatScrollable != snapshot.isScrollable {
                        isChatScrollable = snapshot.isScrollable
                    }
                    updateScrollPosition(isNearBottom: snapshot.isNearBottom)
                }
        } else {
            chatScrollView
        }
    }

    private var chatScrollView: some View {
        ScrollView(showsIndicators: false) {
            LazyVStack(alignment: .leading, spacing: 16) {
                if messages.isEmpty {
                    emptyState
                        .id(Self.emptyStateAnchorID)
                }
                ForEach(messages) { message in
                    MessageRow(
                        message: message,
                        examples: [],
                        onExampleTap: { send($0) },
                        onRetry: retryLast,
                        onProductTap: { openProductDetail($0) },
                        onSpecSubmit: { send($0) },
                        onCompareTap: { comparisonContext = ComparisonContext(candidates: $0) },
                        onFollowUpTap: fillInputWithFollowUp
                    )
                    .id(message.id)
                    .transition(
                        .asymmetric(
                            insertion: .opacity.combined(with: .offset(y: 14)),
                            removal: .opacity
                        )
                    )

                    Color.clear
                        .frame(height: 1)
                        .id(messageTailAnchorID(for: message.id))
                }
                // The composer is an overlay, so the scroll target needs a real spacer
                // instead of trailing padding; otherwise the latest message can hide beneath it.
                Color.clear
                    .frame(height: chatListBottomPadding)
            }
            .padding(.horizontal, 18)
            .padding(.top, 12)
        }
        .scrollDismissesKeyboard(.interactively)
        .simultaneousGesture(
            TapGesture().onEnded {
                isInputFocused = false
            }
        )
        .simultaneousGesture(
            DragGesture(minimumDistance: 8)
                .onChanged { _ in
                    isUserInteractingWithChat = true
                    if isAssistantStreaming {
                        isAutoFollowEnabled = false
                    }
                    if !isNearChatBottom {
                        shouldShowJumpToLatest = true
                    }
                }
                .onEnded { _ in
                    isUserInteractingWithChat = false
                    if isNearChatBottom {
                        isAutoFollowEnabled = true
                        shouldShowJumpToLatest = false
                    } else {
                        isAutoFollowEnabled = false
                        shouldShowJumpToLatest = true
                    }
                }
        )
    }

    @ViewBuilder
    private func jumpToLatestButton(action: @escaping () -> Void) -> some View {
        let label = Image(systemName: "arrow.down")
            .font(.system(size: 18, weight: .bold))
            .foregroundStyle(AppTheme.primary)
            .frame(width: 52, height: 52)

        label
            .background(AppTheme.surface, in: Circle())
            .overlay(
                Circle()
                    .stroke(AppTheme.border, lineWidth: 1)
            )
        .shadow(color: AppTheme.shadow.opacity(0.5), radius: 8, y: 3)
        .contentShape(Circle())
        .highPriorityGesture(
            TapGesture()
                .onEnded(action)
        )
        .accessibilityLabel("跳到最新消息")
        .accessibilityAddTraits(.isButton)
    }

    /// 空态：一句安静的问候 + 朴素的分类入口。仅在当前会话还没有任何消息时显示，
    /// 数据来自后端 /suggestions（真实库存），点哪条都一定有结果；用户发起检索后随消息出现而隐去。
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text("想找点什么")
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                Text("直接说需求，或选个分类")
                    .font(.subheadline)
                    .foregroundStyle(AppTheme.textSecondary)
            }

            FlowLayout(spacing: 10) {
                ForEach(displayedCategories) { category in
                    Button {
                        send(category.query)
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: category.icon)
                                .font(.system(size: 14, weight: .regular))
                                .foregroundStyle(AppTheme.textSecondary)
                            Text(category.name)
                                .font(.subheadline)
                                .foregroundStyle(AppTheme.textPrimary)
                        }
                        .padding(.horizontal, 14)
                        .padding(.vertical, 9)
                        .background(AppTheme.surface, in: Capsule())
                        .overlay(Capsule().stroke(AppTheme.border, lineWidth: 1))
                    }
                    .buttonStyle(.tactile)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 6)
    }

    /// 展示用分类：优先用后端返回，未就绪时退回默认四类，保证空态永不空白。
    private var displayedCategories: [CategoryEntry] {
        let names = suggestions?.categories.isEmpty == false
            ? suggestions!.categories
            : ["数码电子", "服饰运动", "美妆护肤", "食品饮料"]
        return names.map { CategoryEntry(name: $0) }
    }

    /// 展示用热门搜索：优先用后端动态词，未就绪时退回本地示例池。
    private var displayedHotSearches: [String] {
        if let hot = suggestions?.hotSearches, !hot.isEmpty { return hot }
        return examples
    }

    private var composerStack: some View {
        VStack(alignment: .leading, spacing: 10) {
            if isComposerExpanded {
                if #available(iOS 26.0, *) {
                    GlassEffectContainer(spacing: 12) {
                        VStack(alignment: .leading, spacing: 12) {
                            ComposerAction(icon: "camera.fill", title: "相机") { openCamera() }
                            ComposerAction(icon: "photo.fill", title: "图片上传") { openPhotoPicker() }
                        }
                    }
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        ComposerAction(icon: "camera.fill", title: "相机") { openCamera() }
                        ComposerAction(icon: "photo.fill", title: "图片上传") { openPhotoPicker() }
                    }
                    .padding(14)
                    .frame(width: 156, alignment: .leading)
                    .surfacePanel(cornerRadius: 22)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }

            if let data = pendingImageData, let uiImage = UIImage(data: data) {
                HStack(spacing: 8) {
                    ZStack(alignment: .topTrailing) {
                        Image(uiImage: uiImage)
                            .resizable()
                            .scaledToFill()
                            .frame(width: 64, height: 64)
                            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                            .overlay(
                                RoundedRectangle(cornerRadius: 14, style: .continuous)
                                    .stroke(AppTheme.border, lineWidth: 1)
                            )
                        Button {
                            withAnimation(.easeOut(duration: 0.18)) {
                                pendingImageData = nil
                            }
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 18))
                                .symbolRenderingMode(.palette)
                                .foregroundStyle(.white, Color.black.opacity(0.45))
                        }
                        .buttonStyle(.plain)
                        .offset(x: 7, y: -7)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 6)
                .transition(.opacity.combined(with: .move(edge: .bottom)))
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
                        .frame(width: 38, height: 38)
                        .background(AppTheme.primary, in: Circle())
                        .shadow(color: AppTheme.accentGlow, radius: 10, y: 3)
                        .rotationEffect(.degrees(isComposerExpanded ? 45 : 0))
                }
                .buttonStyle(.tactile)

                TextField(composerPlaceholder, text: $inputText)
                    .lineLimit(1)
                    .submitLabel(.send)
                    .onSubmit(sendCurrentInput)
                    .font(.callout)
                    .foregroundStyle(AppTheme.textPrimary)
                    .focused($isInputFocused)

                Button {
                    Task {
                        await speechInput.toggle()
                    }
                } label: {
                    Image(systemName: speechInput.isListening ? "waveform.circle.fill" : "mic.fill")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(speechInput.isListening ? AppTheme.primary : AppTheme.textSecondary)
                        .frame(width: 32, height: 32)
                }
                .buttonStyle(.tactile)

                Button(action: sendCurrentInput) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 38, height: 38)
                        .background(
                            (canSend ? AppTheme.primary : AppTheme.textSecondary.opacity(0.4)),
                            in: Circle()
                        )
                }
                .buttonStyle(.tactile)
                .disabled(!canSend)
                .animation(.easeOut(duration: 0.18), value: canSend)
            }
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .floatingLiquidPanel(cornerRadius: 24)

            if let error = speechInput.errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(AppTheme.error)
                    .padding(.horizontal, 14)
            }
        }
        .onChange(of: speechInput.transcript) { _, newValue in
            if speechInput.isListening {
                inputText = newValue
            }
        }
    }

    private var canSend: Bool {
        !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pendingImageData != nil
    }

    /// 输入框占位文案：挂了图片时引导用户配一句话（如"找个相似但平价的"）。
    private var composerPlaceholder: String {
        pendingImageData != nil ? "补充需求，例如「更平价」「同品牌」" : "搜索商品、品牌或需求"
    }

    private var isKeyboardPresented: Bool {
        keyboardHeight > 0
    }

    private var composerBottomPadding: CGFloat {
        isKeyboardPresented ? 10 : AppTheme.guideComposerBottomPadding
    }

    private var chatListBottomPadding: CGFloat {
        composerHeight + composerBottomPadding + (isKeyboardPresented ? 48 : 104)
    }

    private var jumpButtonBottomPadding: CGFloat {
        composerHeight + composerBottomPadding + 16
    }

    private var latestAssistantMessage: ChatMessage? {
        messages.last { $0.sender == .ai }
    }

    private var isAssistantStreaming: Bool {
        guard let message = latestAssistantMessage else { return false }
        return message.state != .ready && message.state != .failed
    }

    private var shouldKeepLatestQuestionVisible: Bool {
        guard shouldHoldLatestQuestionAnchor, let message = latestAssistantMessage else { return false }
        return isAssistantStreaming
            && message.text.count < questionAnchorCharacterLimit
            && message.products.isEmpty
    }

    private var scrollAnchorToken: String {
        let messageToken = messages.map { message in
            [
                message.id.uuidString,
                message.state.rawValue,
                "\(message.products.count)",
                "\(message.canRetry)"
            ].joined(separator: ":")
        }
        .joined(separator: "|")

        return messageToken
    }

    /// 点击 followup 建议：填入输入框供用户编辑，不直接发送。
    private func fillInputWithFollowUp(_ prompt: String) {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        inputText = trimmed
        isInputFocused = true
    }

    private func sendCurrentInput() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachment = pendingImageData
        // 至少要有文字或图片其一才发送
        guard !trimmed.isEmpty || attachment != nil else { return }
        speechInput.stop()
        inputText = ""
        pendingImageData = nil
        send(trimmed, imageData: attachment)
    }

    private func send(_ query: String, imageData: Data? = nil) {
        isComposerExpanded = false
        speechInput.stop()
        lastQuery = query
        if currentTitle == GuideView.newConversationTitle {
            currentTitle = imageData != nil && query.isEmpty
                ? "拍照找货"
                : String(query.prefix(20))
        }
        let userMessage = ChatMessage(sender: .user, text: query, localImageData: imageData)
        pendingAutoFollowWorkItem?.cancel()
        // 发送后保持自动跟随到最新，确保用户气泡和 AI 回复始终可见、不被输入框遮住。
        isAutoFollowEnabled = true
        isNearChatBottom = true
        isUserInteractingWithChat = false
        shouldShowJumpToLatest = false
        shouldHoldLatestQuestionAnchor = false
        pendingQuestionAnchorID = nil
        let placeholder = imageData != nil ? "正在识别图片并匹配商品" : "正在为你匹配商品"
        // 用户气泡 + 助手占位一起以弹性动画淡入，避免"啪"地直接出现。
        withAnimation(.spring(response: 0.38, dampingFraction: 0.82)) {
            messages.append(userMessage)
            messages.append(ChatMessage(sender: .ai, text: placeholder, state: .understanding))
        }
        runAgent(for: query, imageBase64: imageData?.base64EncodedString())
    }

    private func retryLast() {
        messages.removeAll { $0.canRetry }
        let query = lastQuery.isEmpty ? "重新推荐" : lastQuery
        pendingAutoFollowWorkItem?.cancel()
        isAutoFollowEnabled = true
        shouldHoldLatestQuestionAnchor = false
        pendingQuestionAnchorID = nil
        shouldShowJumpToLatest = false
        messages.append(ChatMessage(sender: .ai, text: "正在重新匹配商品", state: .understanding))
        runAgent(for: query)
    }

    // MARK: - 拍照找货

    private func openCamera() {
        isComposerExpanded = false
        guard UIImagePickerController.isSourceTypeAvailable(.camera) else {
            // 模拟器无相机：退化为相册选择
            showPhotoPicker = true
            return
        }
        showCamera = true
    }

    private func openPhotoPicker() {
        isComposerExpanded = false
        showPhotoPicker = true
    }

    /// 选定/拍摄图片后：压缩并挂到输入区作为待发送附件，弹出键盘让用户补充文字描述。
    private func handlePickedImage(_ data: Data) {
        guard let compressed = Self.compressedJPEG(from: data) else { return }
        isComposerExpanded = false
        withAnimation(.easeOut(duration: 0.2)) {
            pendingImageData = compressed
        }
        isInputFocused = true
    }

    /// 把图片压到最长边 ≤1024px、JPEG 0.7，控制 base64 体积（~200-400KB）。
    private static func compressedJPEG(from data: Data, maxSide: CGFloat = 1024) -> Data? {
        guard let image = UIImage(data: data) else { return nil }
        let side = max(image.size.width, image.size.height)
        let scale = side > maxSide ? maxSide / side : 1.0
        let target = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: target, format: format)
        let resized = renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        return resized.jpegData(compressionQuality: 0.7)
    }

    private func runAgent(for query: String, imageBase64: String? = nil) {
        Task { @MainActor in
            var narrative = ""
            var visibleNarrative = ""
            var isStructuredNarrative = false
            var hydrated: [Product] = []
            var statusText = imageBase64 == nil ? "正在匹配商品" : "正在识别图片并匹配商品"

            do {
                let request = AgentRequestPayload(
                    sessionID: currentSessionID,
                    text: query,
                    imageBase64: imageBase64
                )
                for try await event in agentService.streamResponse(for: request) {
                    switch event.type {
                    case .status:
                        if let status = event.status {
                            statusText = status.message.isEmpty ? statusText : status.message
                            if narrative.isEmpty {
                                updateLastAI(
                                    text: statusText,
                                    state: status.phase.messageState,
                                    products: []
                                )
                            }
                        }

                    case .products:
                        hydrated = event.products.map(Product.init(payload:))
                        updateLastAI(
                            text: narrative.isEmpty ? statusText : narrative,
                            state: .generating,
                            products: []
                        )

                    case .cartSnapshot:
                        if let snapshot = event.cartSnapshot {
                            syncCartItems(from: snapshot)
                        }

                    case .textDelta:
                        if let piece = event.textDelta {
                            narrative += piece
                            let trimmedNarrative = narrative.trimmingCharacters(in: .whitespacesAndNewlines)
                            if isStructuredNarrative || trimmedNarrative.first == "{" {
                                isStructuredNarrative = true
                                updateLastAI(text: statusText, state: .generating)
                            } else {
                                visibleNarrative = await revealTextDelta(
                                    piece,
                                    currentText: visibleNarrative,
                                    fallbackText: statusText
                                )
                            }
                        }

                    case .specSelection:
                        if let spec = event.specSelection {
                            attachSpecSelection(spec)
                        }

                    case .comparison:
                        if let comparison = event.comparison {
                            attachComparison(comparison)
                        }

                    case .done:
                        await finishStreamingResponse(
                            rawText: narrative.isEmpty ? statusText : narrative,
                            visibleText: visibleNarrative,
                            fallbackText: statusText,
                            products: hydrated
                        )

                    default:
                        break
                    }
                }

                // 流正常结束但未收到 done 时的兜底
                if let index = messages.lastIndex(where: { $0.sender == .ai }),
                   messages[index].state != .ready, messages[index].state != .failed {
                    await finishStreamingResponse(
                        rawText: narrative.isEmpty ? statusText : narrative,
                        visibleText: visibleNarrative,
                        fallbackText: statusText,
                        products: hydrated
                    )
                }
                persistCurrent()
                await generateTitleIfNeeded()
            } catch {
                updateLastAI(text: errorMessage(error), state: .failed, canRetry: true)
                persistCurrent()
            }
        }
    }

    private func revealTextDelta(
        _ piece: String,
        currentText: String,
        fallbackText: String
    ) async -> String {
        var nextText = currentText
        for character in piece {
            guard !Task.isCancelled else { return nextText }
            nextText.append(character)
            updateLastAI(
                text: nextText.isEmpty ? fallbackText : nextText,
                state: .generating
            )
            try? await Task.sleep(nanoseconds: streamDelay(for: character))
        }
        return nextText
    }

    private func finishStreamingResponse(
        rawText: String,
        visibleText: String,
        fallbackText: String,
        products: [Product]
    ) async {
        if let content = StructuredContent.parse(from: rawText) {
            await revealStructuredResponse(content, rawText: rawText, products: products)
            return
        }

        var finalVisibleText = visibleText
        if finalVisibleText.isEmpty, rawText != fallbackText {
            finalVisibleText = await revealTextDelta(
                rawText,
                currentText: "",
                fallbackText: fallbackText
            )
        }
        withAnimation(.easeOut(duration: 0.18)) {
            updateLastAI(
                text: finalVisibleText.isEmpty ? rawText : finalVisibleText,
                state: .ready,
                products: products
            )
        }
        markAssistantResponseReadyForScroll()
    }

    private func revealStructuredResponse(
        _ content: StructuredContent,
        rawText: String,
        products: [Product]
    ) async {
        var opening = ""
        for character in content.opening {
            guard !Task.isCancelled else { return }
            opening.append(character)
            updateLastAI(
                text: rawText,
                state: .generating,
                products: [],
                structuredContent: StructuredContent(opening: opening, items: [])
            )
            try? await Task.sleep(nanoseconds: streamDelay(for: character))
        }

        if !products.isEmpty {
            try? await Task.sleep(nanoseconds: streamCardRevealDelay)
        }

        for index in products.indices {
            guard !Task.isCancelled else { return }
            let visibleProducts = Array(products.prefix(index + 1))
            withAnimation(.interactiveSpring(response: 0.34, dampingFraction: 0.86)) {
                updateLastAI(
                    text: rawText,
                    state: .generating,
                    products: visibleProducts,
                    structuredContent: StructuredContent(opening: content.opening, items: content.items)
                )
            }
            if index < (products.indices.last ?? index) {
                try? await Task.sleep(nanoseconds: streamCardRevealDelay)
            }
        }

        withAnimation(.easeOut(duration: 0.18)) {
            updateLastAI(
                text: rawText,
                state: .ready,
                products: products,
                structuredContent: content
            )
        }
        markAssistantResponseReadyForScroll()
    }

    private func markAssistantResponseReadyForScroll() {
        shouldHoldLatestQuestionAnchor = false
        pendingQuestionAnchorID = nil
        isAutoFollowEnabled = true
        shouldShowJumpToLatest = false
        bumpScrollAnchor()
    }

    private func streamDelay(for character: Character) -> UInt64 {
        if character == "\n" {
            return streamLineBreakDelay
        }
        if character.isWhitespace {
            return streamWhitespaceDelay
        }
        if "，。！？、；：,.!?;:".contains(character) {
            return streamPunctuationDelay
        }
        return streamDefaultDelay
    }

    // MARK: - 会话历史

    /// 把当前对话写入本地历史（无用户消息的空白对话会被仓库忽略）。
    private func persistCurrent() {
        let title = currentTitle == GuideView.newConversationTitle
            ? (messages.first { $0.sender == .user }.map { String($0.text.prefix(20)) } ?? GuideView.newConversationTitle)
            : currentTitle
        let createdAt = store.conversation(by: currentConversationID)?.createdAt ?? Date()
        let conversation = Conversation(
            id: currentConversationID,
            sessionID: currentSessionID,
            title: title,
            createdAt: createdAt,
            messages: messages
        )
        store.upsert(conversation)
    }

    /// 首轮对话后用 LLM 生成精炼标题（如「油皮洗面奶」），异步、不阻塞对话；
    /// 仅当标题还是默认/未被用户手动命名时才生成，避免覆盖用户重命名。
    private func generateTitleIfNeeded() async {
        // 已被 LLM 或用户改过（非默认、且不是首条消息的截句）就不再生成。
        guard currentTitle == GuideView.newConversationTitle
            || isAutoTruncatedTitle else { return }
        guard let firstUser = messages.first(where: { $0.sender == .user })?.text,
              !firstUser.isEmpty else { return }
        let firstAI = messages.first { $0.sender == .ai && $0.state == .ready }?.text
        let convoID = currentConversationID
        guard let title = await productService.fetchTitle(userText: firstUser, assistantText: firstAI),
              !title.isEmpty else { return }
        // 异步返回期间用户可能已切换对话，确认还在同一对话才回写。
        guard convoID == currentConversationID else {
            // 直接更新历史里那条对话的标题
            if var convo = store.conversation(by: convoID) {
                convo.title = title
                store.upsert(convo)
            }
            return
        }
        currentTitle = title
        persistCurrent()
    }

    /// 当前标题是否是「首条消息截句」自动生成的（可被 LLM 标题替换）。
    private var isAutoTruncatedTitle: Bool {
        guard let firstUser = messages.first(where: { $0.sender == .user })?.text else { return false }
        return currentTitle == String(firstUser.prefix(20))
    }

    /// 开启全新对话：存档当前 → 清空 → 换新 session_id（后端会 mint 新会话）。
    private func startNewConversation() {
        persistCurrent()
        resetScrollFollowState()
        messages = []
        examples = GuideView.freshExamples()
        currentConversationID = UUID()
        currentSessionID = UUID().uuidString
        currentTitle = GuideView.newConversationTitle
        lastQuery = ""
        showHistory = false
        chatListIdentity = UUID()
        // 回到空态时换一批热门搜索，保持「动态」观感。
        Task { await loadSuggestions() }
    }

    /// 拉取空态首页推荐（分类入口 + 动态热门搜索，均源自真实库存）。
    @MainActor
    private func loadSuggestions() async {
        if let fresh = await productService.fetchSuggestions() {
            withAnimation(.easeInOut(duration: 0.25)) {
                suggestions = fresh
            }
        }
    }

    /// 「换一批」：手动刷新热门搜索词。
    @MainActor
    private func refreshHotSearches() async {
        guard !isRefreshingHot else { return }
        isRefreshingHot = true
        defer { isRefreshingHot = false }
        await loadSuggestions()
    }

    /// 重开历史对话：存档当前 → 载入选中会话（含其 session_id，可继续追问）。
    private func openConversation(_ conversation: Conversation) {
        persistCurrent()
        resetScrollFollowState()
        messages = conversation.messages
        currentConversationID = conversation.id
        currentSessionID = conversation.sessionID
        currentTitle = conversation.title
        lastQuery = conversation.messages.last { $0.sender == .user }?.text ?? ""
        showHistory = false
        chatListIdentity = UUID()
    }

    private func syncCartItems(from snapshot: CartSnapshotPayload) {
        cartItems = snapshot.items.map(CartItem.init(payload:))
    }

    private func errorMessage(_ error: Error) -> String {
        if let restError = error as? RESTServiceError {
            return detailedErrorMessage(for: restError)
        }
        return defaultErrorMessage()
    }
    
    private func detailedErrorMessage(for error: RESTServiceError) -> String {
        var message = ""
        var errorMessage = ""
        
        switch error {
        case .invalidURL:
            message += "服务地址无效\n\n"
            errorMessage = "商品服务地址无效。"
        case .invalidResponse:
            message += "服务响应无效\n\n"
            errorMessage = "商品服务返回了无效响应。"
        case .requestFailed(let code, _, let msg):
            errorMessage = msg
            switch code {
            case 400:
                message += "请求参数有误\n\n"
            case 401, 403:
                message += "认证失败，请检查网络权限\n\n"
            case 404:
                message += "找不到相关商品\n\n"
            case 429:
                message += "请求过于频繁，请稍候再试\n\n"
            case 500...599:
                message += "服务器暂时不可用\n\n"
            default:
                message += "商品服务暂时不可用\n\n"
            }
        case .connectionFailed:
            message += "连接失败\n\n"
            errorMessage = "商品服务暂时不可用，请确认后端 REST API 已启动。"
        case .decodingFailed:
            message += "数据解析失败\n\n"
            errorMessage = "商品服务返回数据解析失败。"
        }
        
        message += "失败原因：\(errorMessage)\n\n"
        
        message += "建议：\n"
        message += "• 检查网络连接是否正常\n"
        message += "• 尝试简化搜索关键词\n"
        message += "• 稍后重新尝试\n"
        
        return message
    }
    
    private func defaultErrorMessage() -> String {
        return "商品服务暂时不可用\n\n" +
               "失败原因：未知错误\n\n" +
               "建议：\n" +
               "• 检查网络连接是否正常\n" +
               "• 尝试简化搜索关键词\n" +
               "• 点击下方按钮重新尝试"
    }

    private func updateKeyboardHeight(from notification: Notification) {
        guard let frame = notification.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect else {
            keyboardHeight = 0
            return
        }

        let screenHeight = UIApplication.shared.connectedScenes
            .compactMap { ($0 as? UIWindowScene)?.screen.bounds.height }
            .first ?? frame.maxY
        let height = max(0, screenHeight - frame.minY)
        let duration = notification.userInfo?[UIResponder.keyboardAnimationDurationUserInfoKey] as? Double ?? 0.25

        withAnimation(.easeOut(duration: duration)) {
            keyboardHeight = height
        }
    }

    private func resetScrollFollowState() {
        pendingAutoFollowWorkItem?.cancel()
        pendingQuestionAnchorID = nil
        shouldHoldLatestQuestionAnchor = false
        isAutoFollowEnabled = true
        isNearChatBottom = true
        isUserInteractingWithChat = false
        shouldShowJumpToLatest = false
    }

    private func updateScrollPosition(isNearBottom: Bool) {
        guard isNearChatBottom != isNearBottom else { return }
        isNearChatBottom = isNearBottom

        if isNearBottom {
            shouldShowJumpToLatest = false
            if !isUserInteractingWithChat {
                isAutoFollowEnabled = true
            }
        } else if isUserInteractingWithChat {
            isAutoFollowEnabled = false
            shouldShowJumpToLatest = true
        } else if !isAutoFollowEnabled {
            shouldShowJumpToLatest = true
        }
    }

    private func handleContentChange(_ proxy: ScrollViewProxy) {
        shouldHoldLatestQuestionAnchor = false
        if isAutoFollowEnabled {
            scheduleAutoFollowScroll(proxy, animated: true)
        } else if !isNearChatBottom {
            shouldShowJumpToLatest = true
        }
    }

    /// 消息/状态变化时驱动滚动：
    /// - 刚发送（短回复流式中）：把用户这条问题滚到顶部，让用户从头读，AI 回复在下方展开。
    /// - 其它情况（长回复、出商品卡、已就绪）：跟随到最新内容底部，确保新内容不被输入框遮住。
    private func driveAutoScroll(_ proxy: ScrollViewProxy) {
        if let message = latestAssistantMessage, message.state == .ready || message.state == .failed {
            forceScrollToLatestMessage(proxy)
            return
        }
        handleContentChange(proxy)
    }

    private func scrollToQuestion(_ id: UUID, proxy: ScrollViewProxy) {
        pendingAutoFollowWorkItem?.cancel()
        DispatchQueue.main.async {
            withAnimation(.easeOut(duration: 0.24)) {
                proxy.scrollTo(id, anchor: .top)
            }
        }
    }

    private func jumpToLatest(_ proxy: ScrollViewProxy) {
        pendingAutoFollowWorkItem?.cancel()
        shouldHoldLatestQuestionAnchor = false
        isAutoFollowEnabled = true
        shouldShowJumpToLatest = false
        scrollToLatestMessage(proxy, animated: true)
        scheduleAutoFollowScroll(proxy, animated: true, delay: 0.12)
    }

    private func scrollToEmptyState(_ proxy: ScrollViewProxy) {
        guard messages.isEmpty else { return }
        DispatchQueue.main.async {
            proxy.scrollTo(Self.emptyStateAnchorID, anchor: .top)
        }
    }

    private func scheduleAutoFollowScroll(
        _ proxy: ScrollViewProxy,
        animated: Bool,
        delay: TimeInterval = 0.05
    ) {
        guard isAutoFollowEnabled else { return }
        pendingAutoFollowWorkItem?.cancel()

        let workItem = DispatchWorkItem {
            scrollToLatestMessage(proxy, animated: animated)
        }
        pendingAutoFollowWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }

    private func forceScrollToLatestMessage(_ proxy: ScrollViewProxy) {
        pendingAutoFollowWorkItem?.cancel()
        shouldHoldLatestQuestionAnchor = false
        isAutoFollowEnabled = true
        shouldShowJumpToLatest = false

        let delays: [TimeInterval] = [0.01, 0.16, 0.36]
        for delay in delays {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                scrollToLatestMessage(proxy, animated: true)
            }
        }
    }

    private func scrollToLatestMessage(_ proxy: ScrollViewProxy, animated: Bool) {
        guard let latestMessageID = messages.last?.id else { return }
        let targetID = messageTailAnchorID(for: latestMessageID)
        let anchor = latestContentAnchor

        if animated {
            withAnimation(.interactiveSpring(response: 0.32, dampingFraction: 0.86, blendDuration: 0.08)) {
                proxy.scrollTo(targetID, anchor: anchor)
            }
        } else {
            proxy.scrollTo(targetID, anchor: anchor)
        }
    }

    private var latestContentAnchor: UnitPoint {
        UnitPoint(x: 0.5, y: isKeyboardPresented ? 0.46 : 0.58)
    }

    private func messageTailAnchorID(for messageID: UUID) -> String {
        "guide-message-tail-\(messageID.uuidString)"
    }

    private func updateLastAI(
        text: String,
        state: MessageState,
        products: [Product] = [],
        canRetry: Bool = false,
        structuredContent: StructuredContent? = nil
    ) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].text = text
        messages[index].state = state
        messages[index].products = products
        messages[index].canRetry = canRetry
        if let structuredContent {
            messages[index].structuredContent = structuredContent
        }
    }

    /// 把后端「请选择规格」的可交互卡片挂到当前 AI 消息上；后续 token/done
    /// 只更新文本与状态，不会清掉已挂上的卡片。
    private func attachSpecSelection(_ spec: SpecSelection) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].specSelection = spec
    }

    /// 把对比结果挂到当前 AI 消息上，对话流里直接渲染对比卡片。
    private func attachComparison(_ comparison: ProductComparisonPayload) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].comparison = comparison
    }

    private func openProductDetail(_ product: Product) {
        productDetailContext = ProductDetailContext(product: product)
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
        Task {
            if let snapshot = try? await productService.mutateAgentCart(
                CartMutationRequest(
                    action: .add,
                    productID: product.id,
                    skuID: product.matchingSKU(for: selectedOptions)?.id ?? product.displaySKU(for: selectedOptions)?.id,
                    selectedOptions: selectedOptions,
                    quantity: quantity
                )
            ) {
                await MainActor.run { syncCartItems(from: snapshot) }
            }
        }
    }
}

struct MessageRow: View {
    let message: ChatMessage
    let examples: [String]
    let onExampleTap: (String) -> Void
    let onRetry: () -> Void
    let onProductTap: (Product) -> Void
    let onSpecSubmit: (String) -> Void
    let onCompareTap: ([Product]) -> Void
    let onFollowUpTap: (String) -> Void
    @State private var dotCount = 0
    
    private struct ProductSection: Identifiable, Equatable {
        let id: String
        let product: Product
        let description: String?
        
        init(product: Product, description: String?) {
            self.id = "\(product.id)_\(description?.hashValue ?? 0)"
            self.product = product
            self.description = description
        }
        
        static func == (lhs: ProductSection, rhs: ProductSection) -> Bool {
            lhs.product.id == rhs.product.id && lhs.description == rhs.description
        }
    }
    
    private var parsedStructuredContent: StructuredContent? {
        guard let structured = message.structuredContent else {
            guard message.state == .ready else {
                return nil
            }
            return tryParseStructuredContent(from: message.text)
        }
        return structured
    }
    
    private func tryParseStructuredContent(from text: String) -> StructuredContent? {
        StructuredContent.parse(from: text)
    }
    
    private var textParagraphs: [String] {
        let components = message.text.components(separatedBy: "\n")
        return components.filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
    
    private var openingText: String? {
        // 优先使用结构化内容
        if let content = parsedStructuredContent {
            return content.opening
        }
        // 降级：从文本中取第一段
        return textParagraphs.first
    }
    
    private var followups: [String] {
        guard message.sender == .ai else { return [] }
        guard let content = parsedStructuredContent else { return [] }
        return content.followup.filter {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        // 降级：从文本中提取真正像「追问方向」的段落。
        // 注意：必须排除已作为开场白渲染的段落，否则像 cart 选规格这类单段确定性
        // 文案（含「想要」字样）会被同一段既渲染成开场白气泡、又渲染成追问胶囊 →
        // 屏幕上出现一模一样的两个气泡。开场白已经展示过，不再重复当追问。
        let opening = openingText
        let questions = textParagraphs.filter { paragraph in
            guard paragraph != opening else { return false }
            return paragraph.contains("？") || paragraph.contains("?") ||
                paragraph.contains("需要") || paragraph.contains("想要") ||
                paragraph.contains("想看")
        }
        return questions
    }
    
    private var middleParagraphs: [String] {
        // 如果有结构化内容，不使用 middleParagraphs
        if parsedStructuredContent != nil {
            return []
        }
        // 降级：从文本中提取中间段落
        guard let opening = openingText else { return textParagraphs }
        var middle = textParagraphs.filter { $0 != opening }
        if !message.products.isEmpty && middle.count > message.products.count {
            middle = Array(middle.prefix(message.products.count))
        }
        return middle
    }
    
    private var productSections: [ProductSection] {
        let products = message.products
        
        // 结构化内容：优先 productId 精确匹配，失败则按顺序对齐（兼容 LLM 返回序号的情况）
        if let content = parsedStructuredContent {
            return products.enumerated().map { index, product in
                let item = content.items.first { $0.productId == product.id }
                    ?? (index < content.items.count ? content.items[index] : nil)
                let description = item.flatMap {
                    let cleaned = RecommendationCopy.sanitized($0.description)
                    return cleaned.isEmpty ? nil : cleaned
                } ?? (product.reason.isEmpty ? nil : product.reason)
                return ProductSection(product: product, description: description)
            }
        }
        
        // 否则使用索引方式匹配（降级方案）
        let descriptions = middleParagraphs
        return products.enumerated().map { index, product in
            ProductSection(
                product: product,
                description: index < descriptions.count ? descriptions[index] : product.reason
            )
        }
    }
    
    var body: some View {
        HStack(alignment: .top) {
            if message.sender == .user { Spacer(minLength: 44) }

            VStack(alignment: message.sender == .user ? .trailing : .leading, spacing: 10) {
                if let data = message.localImageData, let uiImage = UIImage(data: data) {
                    Image(uiImage: uiImage)
                        .resizable()
                        .scaledToFill()
                        .frame(width: 160, height: 160)
                        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .stroke(AppTheme.border, lineWidth: 1)
                        )
                }

                if message.state == .ready || isRevealingStructuredContent {
                    if message.sender == .user {
                        Text(message.text)
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.textPrimary)
                            .lineSpacing(6)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 11)
                            .background(bubbleColor, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    } else {
                    // 1. 开场白
                    if let opening = openingText {
                        Text(opening)
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.textPrimary)
                            .lineSpacing(6)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 11)
                            .background(bubbleColor, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                            .overlay(
                                RoundedRectangle(cornerRadius: 18, style: .continuous)
                                    .stroke(AppTheme.border, lineWidth: message.sender == .ai ? 1 : 0)
                            )
                    }

                    if parsedStructuredContent == nil && message.products.isEmpty {
                        ForEach(Array(middleParagraphs.enumerated()), id: \.offset) { _, paragraph in
                            Text(paragraph)
                                .font(.subheadline)
                                .foregroundStyle(AppTheme.textPrimary)
                                .lineSpacing(6)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 11)
                                .background(bubbleColor, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                                        .stroke(AppTheme.border, lineWidth: message.sender == .ai ? 1 : 0)
                                )
                        }
                    }
                    
                    // 2. 商品卡片 + 解说交替展示
                    ForEach(productSections) { section in
                        ProductCard(product: section.product) {
                            onProductTap(section.product)
                        }
                        .transition(.opacity.combined(with: .scale).combined(with: .offset(y: 20)))

                        if let description = section.description {
                            Text(description)
                                .font(.subheadline)
                                .foregroundStyle(AppTheme.textSecondary)
                                .lineSpacing(4)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 8)
                                .transition(.opacity)
                        }
                    }
                    
                    // 3. 追问 Prompt（点击填入输入框，不直接发送）
                    if !followups.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(followups, id: \.self) { prompt in
                                Button(action: {
                                    onFollowUpTap(prompt)
                                }) {
                                    Text(prompt)
                                        .font(.subheadline)
                                        .foregroundStyle(AppTheme.primary)
                                        .padding(.horizontal, 14)
                                        .padding(.vertical, 8)
                                        .background(AppTheme.secondary.opacity(0.7), in: Capsule())
                                        .overlay(Capsule().stroke(AppTheme.border, lineWidth: 1))
                                }
                                .buttonStyle(.tactile)
                            }
                        }
                        .padding(.top, 8)
                    }
                    }
                } else if shouldShowStreamingPlainText {
                    Text(message.text)
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineSpacing(6)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 11)
                        .background(bubbleColor, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .stroke(AppTheme.border, lineWidth: message.sender == .ai ? 1 : 0)
                        )
                } else if !displayText.isEmpty {
                    loadingText
                }

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
                                .buttonStyle(.tactile)
                        }
                    }
                }

                if message.canRetry {
                    VStack(spacing: 12) {
                        Button(action: onRetry) {
                            HStack(spacing: 8) {
                                Image(systemName: "arrow.clockwise")
                                    .font(.headline)
                                Text("重新尝试")
                                    .font(.headline.weight(.semibold))
                            }
                            .padding(.horizontal, 24)
                            .padding(.vertical, 14)
                            .background(AppTheme.primary)
                            .foregroundColor(.white)
                            .cornerRadius(14)
                            .shadow(color: AppTheme.primary.opacity(0.22), radius: 6, x: 0, y: 3)
                        }
                        
                        Text("点击上方按钮重新搜索")
                            .font(.caption)
                            .foregroundStyle(AppTheme.textSecondary)
                    }
                    .padding(.top, 4)
                }

                if let spec = message.specSelection {
                    SpecSelectionCard(selection: spec, onSubmit: onSpecSubmit)
                }

                if let comparison = message.comparison {
                    ComparisonCard(comparison: comparison)
                }

                // 对话结束、推荐了 ≥2 件商品时，给个小按钮进对比页（页内下拉选商品）。
                if message.sender == .ai, message.state == .ready, message.products.count >= 2 {
                    Button {
                        onCompareTap(message.products)
                    } label: {
                        Label("对比商品", systemImage: "square.split.2x1")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(AppTheme.primary)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                            .background(AppTheme.softPurple, in: Capsule())
                    }
                    .buttonStyle(.tactile)
                }
            }

            if message.sender == .ai { Spacer(minLength: 44) }
        }
    }
    
    @ViewBuilder
    private var loadingText: some View {
        switch message.state {
        case .ready, .failed:
            if message.state == .failed {
                VStack(spacing: 12) {
                    HStack(spacing: 10) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.title2)
                            .foregroundColor(AppTheme.error)
                        Text("出错了")
                            .font(.headline)
                            .foregroundColor(AppTheme.error)
                    }
                    .padding(.top, 4)
                    
                    Text(message.text)
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineSpacing(6)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 14)
                .background(bubbleColor, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .stroke(AppTheme.error.opacity(0.3), lineWidth: 2)
                )
            } else {
                // 首字符优化：文本很短时继续显示加载动画，避免闪烁
                if message.text.count < 10 {
                    HStack(spacing: 2) {
                        Text(message.state.rawValue)
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.textPrimary)
                        Text(String(repeating: ".", count: dotCount))
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.textPrimary)
                            .monospacedDigit()
                    }
                    .padding(.horizontal, 4)
                    .onAppear {
                        startLoadingAnimation()
                    }
                } else {
                    Text(message.text)
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineSpacing(6)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 11)
                        .background(bubbleColor, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .stroke(AppTheme.border, lineWidth: message.sender == .ai ? 1 : 0)
                        )
                }
            }
        case .understanding, .retrieving, .generating:
            HStack(spacing: 2) {
                Text(message.state.rawValue)
                    .font(.subheadline)
                    .foregroundStyle(AppTheme.textPrimary)
                Text(String(repeating: ".", count: dotCount))
                    .font(.subheadline)
                    .foregroundStyle(AppTheme.textPrimary)
                    .monospacedDigit()
            }
            .padding(.horizontal, 4)
            .onAppear {
                startLoadingAnimation()
            }
        }
    }
    
    private func startLoadingAnimation() {
        dotCount = 0
        Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { timer in
            if message.state == .ready || message.state == .failed {
                timer.invalidate()
                return
            }
            withAnimation(.easeInOut(duration: 0.2)) {
                dotCount = (dotCount + 1) % 4
            }
        }
    }

    private var displayText: String {
        switch message.state {
        case .ready, .failed:
            return message.text
        default:
            return message.text
        }
    }

    private var isRevealingStructuredContent: Bool {
        message.sender == .ai && message.state == .generating && message.structuredContent != nil
    }

    private var shouldShowStreamingPlainText: Bool {
        guard message.sender == .ai, message.state == .generating else { return false }
        let trimmed = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        guard message.structuredContent == nil else { return false }
        return !trimmed.hasPrefix("正在")
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

@MainActor
final class SpeechInputController: NSObject, ObservableObject, SFSpeechRecognizerDelegate {
    @Published private(set) var isListening = false
    @Published private(set) var transcript = ""
    @Published private(set) var errorMessage: String?

    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh_CN"))
    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    override init() {
        super.init()
        speechRecognizer?.delegate = self
    }

    func toggle() async {
        if isListening {
            stop()
        } else {
            await start()
        }
    }

    func start() async {
        errorMessage = nil
        guard speechRecognizer?.isAvailable == true else {
            errorMessage = "语音识别暂时不可用。"
            return
        }
        guard await requestSpeechAuthorization() else {
            errorMessage = "请允许语音识别权限后再使用语音输入。"
            return
        }
        guard await requestMicrophoneAuthorization() else {
            errorMessage = "请允许麦克风权限后再使用语音输入。"
            return
        }

        recognitionTask?.cancel()
        recognitionTask = nil
        transcript = ""

        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.record, mode: .measurement, options: .duckOthers)
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)

            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            recognitionRequest = request

            let inputNode = audioEngine.inputNode
            inputNode.removeTap(onBus: 0)
            let recordingFormat = inputNode.outputFormat(forBus: 0)
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak request] buffer, _ in
                request?.append(buffer)
            }

            audioEngine.prepare()
            try audioEngine.start()
            isListening = true

            recognitionTask = speechRecognizer?.recognitionTask(with: request) { [weak self] result, error in
                Task { @MainActor in
                    guard let self else { return }
                    if let result {
                        self.transcript = result.bestTranscription.formattedString
                        if result.isFinal {
                            self.stop()
                        }
                    }
                    if error != nil {
                        self.stop()
                    }
                }
            }
        } catch {
            stop()
            errorMessage = "语音输入启动失败，请稍后重试。"
        }
    }

    func stop() {
        if audioEngine.isRunning {
            audioEngine.stop()
            audioEngine.inputNode.removeTap(onBus: 0)
        }
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionRequest = nil
        recognitionTask = nil
        isListening = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func requestSpeechAuthorization() async -> Bool {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status == .authorized)
            }
        }
    }

    private func requestMicrophoneAuthorization() async -> Bool {
        await withCheckedContinuation { continuation in
            if #available(iOS 17.0, *) {
                AVAudioApplication.requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            } else {
                AVAudioSession.sharedInstance().requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
        }
    }

    nonisolated func speechRecognizer(
        _ speechRecognizer: SFSpeechRecognizer,
        availabilityDidChange available: Bool
    ) {
        if !available {
            Task { @MainActor in
                stop()
                errorMessage = "语音识别暂时不可用。"
            }
        }
    }
}
struct ComposerAction: View {
    let icon: String
    let title: String
    var action: () -> Void = {}

    var body: some View {
        if #available(iOS 26.0, *) {
            Button(action: action) {
                label
            }
            .buttonStyle(.plain)
            .padding(.vertical, 10)
            .padding(.horizontal, 16)
            .glassEffect(.regular.interactive(), in: Capsule())
        } else {
            Button(action: action) {
                label
            }
            .buttonStyle(.plain)
        }
    }

    private var label: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(AppTheme.primary)
                .frame(width: 26, height: 26)
                .background(AppTheme.primary.opacity(0.12), in: Circle())
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
                .frame(height: 248)
                .background(Color.white, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .stroke(AppTheme.border, lineWidth: 1)
                )

            if !product.tags.isEmpty {
                HStack(spacing: 8) {
                    ForEach(Array(product.tags.prefix(3)), id: \.self) { tag in
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
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            Text(product.priceDisplay(for: product.defaultSpecificationSelection))
                .font(.title3.bold())
                .foregroundStyle(AppTheme.error)

            Button(action: onDetail) {
                HStack(spacing: 6) {
                    Text("查看详情")
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.bold))
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 11)
                .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            .buttonStyle(.tactile)
        }
        .padding(14)
        .surfacePanel(cornerRadius: 22)
    }
}

/// 多规格商品加购时的交互卡片：逐维度点选规格值，选齐后一键加入购物车。
/// 提交时把选择组合成自然语言（如「我要 黑色 Black 42码」）发回后端完成精确加购。
struct SpecSelectionCard: View {
    let selection: SpecSelection
    let onSubmit: (String) -> Void

    @State private var selected: [String: String] = [:]
    @State private var submitted = false

    private var allChosen: Bool {
        selection.dimensions.allSatisfy { selected[$0.name] != nil }
    }

    private var buttonTitle: String {
        if submitted { return "已选择" }
        return allChosen ? "加入购物车" : "请选择规格"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(selection.dimensions) { dimension in
                VStack(alignment: .leading, spacing: 8) {
                    Text(dimension.name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)
                    FlowLayout(spacing: 8) {
                        ForEach(dimension.values, id: \.self) { value in
                            chip(dimension: dimension.name, value: value)
                        }
                    }
                }
            }

            Button(action: submit) {
                Text(buttonTitle)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 11)
                    .background(
                        (allChosen && !submitted) ? AppTheme.primary : AppTheme.primary.opacity(0.4),
                        in: RoundedRectangle(cornerRadius: 14, style: .continuous)
                    )
            }
            .buttonStyle(.tactile)
            .disabled(!allChosen || submitted)
        }
        .padding(14)
        .frame(maxWidth: 320, alignment: .leading)
        .surfacePanel(cornerRadius: 22)
    }

    private func chip(dimension: String, value: String) -> some View {
        let isSelected = selected[dimension] == value
        return Button {
            guard !submitted else { return }
            selected[dimension] = value
        } label: {
            Text(value)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(isSelected ? .white : AppTheme.primary)
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .background(isSelected ? AppTheme.primary : AppTheme.softPurple, in: Capsule())
        }
        .disabled(submitted)
        .buttonStyle(.tactile)
    }

    private func submit() {
        guard allChosen, !submitted else { return }
        submitted = true
        let phrase = "我要 " + selection.dimensions
            .compactMap { selected[$0.name] }
            .joined(separator: " ")
        onSubmit(phrase)
    }
}

/// 空态分类入口的展示模型：按分类名映射图标、主题色与点击后发送的 query。
struct CategoryEntry: Identifiable {
    let name: String
    var id: String { name }

    /// 点击后发送给 Agent 的检索语：自然口吻，落到对应分类。
    var query: String { "推荐\(name)" }

    var icon: String {
        switch name {
        // 统一描边线条风格的简约图标。
        case "数码电子": return "laptopcomputer"
        case "服饰运动": return "figure.run"
        case "美妆护肤": return "drop"
        case "食品饮料", "食品生活": return "cup.and.saucer"
        default: return "bag"
        }
    }

    var tint: Color {
        switch name {
        // 低饱和大地色，与陶土主色和谐，去掉高饱和 AI 亮色。
        case "数码电子": return Color(hex: "6E89A6")   // 雾蓝
        case "服饰运动": return Color(hex: "7E9B6B")   // 橄榄绿
        case "美妆护肤": return Color(hex: "C77B82")   // 豆沙粉
        case "食品饮料", "食品生活": return Color(hex: "D69A4C")   // 芥末黄
        default: return AppTheme.primary
        }
    }
}

/// 轻量流式布局：子视图按行从左到右排列，超出宽度自动换行（用于规格 chip）。
struct FlowLayout: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0
        var rowHeight: CGFloat = 0
        var totalHeight: CGFloat = 0
        var maxRowWidth: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > maxWidth, x > 0 {
                totalHeight += rowHeight + spacing
                maxRowWidth = max(maxRowWidth, x - spacing)
                x = 0
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        totalHeight += rowHeight
        maxRowWidth = max(maxRowWidth, x - spacing)
        let width = maxWidth.isFinite ? min(maxRowWidth, maxWidth) : maxRowWidth
        return CGSize(width: max(width, 0), height: totalHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x: CGFloat = bounds.minX
        var y: CGFloat = bounds.minY
        var rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

/// 相机拍照选择器：包装 UIImagePickerController（SwiftUI 无原生相机入口）。
struct CameraPicker: UIViewControllerRepresentable {
    let onImage: (UIImage) -> Void
    @Environment(\.dismiss) private var dismiss

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        private let parent: CameraPicker

        init(_ parent: CameraPicker) {
            self.parent = parent
        }

        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
        ) {
            if let image = info[.originalImage] as? UIImage {
                parent.onImage(image)
            }
            parent.dismiss()
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            parent.dismiss()
        }
    }
}

struct HistorySheet: View {
    let conversations: [Conversation]
    let onSelect: (Conversation) -> Void
    let onDelete: (Conversation) -> Void
    let onRename: (Conversation, String) -> Void
    let onClearAll: () -> Void

    @State private var pendingDelete: Conversation?
    @State private var renameTarget: Conversation?
    @State private var renameText: String = ""
    @State private var searchText: String = ""
    @State private var showClearAllConfirm = false

    /// 按标题 + 消息内容过滤（不区分大小写）。
    private var filtered: [Conversation] {
        let q = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return conversations }
        return conversations.filter { convo in
            if convo.title.lowercased().contains(q) { return true }
            return convo.messages.contains { $0.text.lowercased().contains(q) }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("历史记录")
                    .font(.title3.bold())
                Spacer()
                if !conversations.isEmpty {
                    Button(role: .destructive) {
                        showClearAllConfirm = true
                    } label: {
                        Label("清空", systemImage: "trash")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(AppTheme.error)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 16)

            if !conversations.isEmpty {
                searchBar
            }

            if conversations.isEmpty {
                emptyState
            } else if filtered.isEmpty {
                noResultState
            } else {
                List {
                    ForEach(filtered) { conversation in
                        Button {
                            onSelect(conversation)
                        } label: {
                            row(conversation)
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                        // 左滑：重命名 + 删除
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button(role: .destructive) {
                                onDelete(conversation)
                            } label: {
                                Label("删除", systemImage: "trash")
                            }
                            Button {
                                startRename(conversation)
                            } label: {
                                Label("重命名", systemImage: "pencil")
                            }
                            .tint(AppTheme.primary)
                        }
                        // 长按弹出菜单：重命名 / 删除
                        .contextMenu {
                            Button {
                                startRename(conversation)
                            } label: {
                                Label("重命名", systemImage: "pencil")
                            }
                            Button(role: .destructive) {
                                pendingDelete = conversation
                            } label: {
                                Label("删除对话", systemImage: "trash")
                            }
                        }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(AppTheme.background)
        .confirmationDialog(
            "删除这段对话？",
            isPresented: Binding(
                get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("删除", role: .destructive) {
                if let target = pendingDelete { onDelete(target) }
                pendingDelete = nil
            }
            Button("取消", role: .cancel) { pendingDelete = nil }
        } message: {
            Text(pendingDelete?.title ?? "")
        }
        .alert("重命名对话", isPresented: Binding(
            get: { renameTarget != nil },
            set: { if !$0 { renameTarget = nil } }
        )) {
            TextField("对话标题", text: $renameText)
            Button("保存") {
                if let target = renameTarget {
                    onRename(target, renameText)
                }
                renameTarget = nil
            }
            Button("取消", role: .cancel) { renameTarget = nil }
        }
        .confirmationDialog(
            "清空所有历史对话？此操作不可撤销",
            isPresented: $showClearAllConfirm,
            titleVisibility: .visible
        ) {
            Button("清空全部 \(conversations.count) 段对话", role: .destructive) { onClearAll() }
            Button("取消", role: .cancel) {}
        }
    }

    private var searchBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
            TextField("搜索历史对话", text: $searchText)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
                .submitLabel(.search)
            if !searchText.isEmpty {
                Button {
                    searchText = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textSecondary)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(AppTheme.surface, in: Capsule())
        .overlay(Capsule().stroke(AppTheme.border, lineWidth: 1))
        .padding(.horizontal, 20)
    }

    private func startRename(_ conversation: Conversation) {
        renameText = conversation.title
        renameTarget = conversation
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "clock.arrow.circlepath")
                .font(.largeTitle)
                .foregroundStyle(AppTheme.textSecondary)
            Text("还没有历史对话")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var noResultState: some View {
        VStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.largeTitle)
                .foregroundStyle(AppTheme.textSecondary)
            Text("没有匹配的对话")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func row(_ conversation: Conversation) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "message")
                .foregroundStyle(AppTheme.primary)
                .frame(width: 40, height: 40)
                .background(AppTheme.softPurple, in: Circle())
            VStack(alignment: .leading, spacing: 4) {
                Text(conversation.title).font(.headline).lineLimit(1)
                Text(conversation.subtitle).font(.caption).foregroundStyle(AppTheme.textSecondary)
            }
            Spacer()
            Image(systemName: "chevron.right")
                .font(.caption.weight(.bold))
                .foregroundStyle(AppTheme.textSecondary)
        }
        .padding(12)
        .floatingLiquidPanel(cornerRadius: 18)
    }
}

private extension AgentStatusPhase {
    var messageState: MessageState {
        switch self {
        case .understanding: return .understanding
        case .retrieving, .executingTool: return .retrieving
        case .generating: return .generating
        case .done: return .ready
        }
    }
}

#if DEBUG
#Preview("推荐消息") {
    ScrollView {
        MessageRow(
            message: PreviewFixtures.recommendMessage,
            examples: [],
            onExampleTap: { _ in },
            onRetry: {},
            onProductTap: { _ in },
            onSpecSubmit: { _ in },
            onCompareTap: { _ in },
            onFollowUpTap: { _ in }
        )
        .padding()
    }
    .background(AppTheme.background)
}

#Preview("加载中") {
    MessageRow(
        message: PreviewFixtures.loadingMessage,
        examples: [],
        onExampleTap: { _ in },
        onRetry: {},
        onProductTap: { _ in },
        onSpecSubmit: { _ in },
        onCompareTap: { _ in },
        onFollowUpTap: { _ in }
    )
    .padding()
    .background(AppTheme.background)
}
#endif
