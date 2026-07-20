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

/// Chat scroll snapshot indicating whether the view is near the bottom and whether content exceeds one screen.
private struct ScrollSnapshot: Equatable {
    let isNearBottom: Bool
    let isScrollable: Bool
}

/// Context for opening the comparison screen with products recommended by the current AI message.
/// The comparison screen lets users select two or three candidates from dropdown menus.
struct ComparisonContext: Identifiable, Hashable {
    let id = UUID()
    let candidates: [Product]
}

struct GuideView: View {
    @Binding var cartItems: [CartItem]
    @ObservedObject var preferenceStore: PreferenceStore
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
    /// Whether content exceeds the viewport. The jump-to-latest control appears only when scrolling is possible and not at the bottom.
    @State private var isChatScrollable = false
    @State private var pendingQuestionAnchorID: UUID?
    @State private var shouldHoldLatestQuestionAnchor = false
    @State private var pendingAutoFollowWorkItem: DispatchWorkItem?
    @State private var photoPickerItem: PhotosPickerItem?
    @State private var showPhotoPicker = false
    @State private var showCamera = false
    @State private var chatListIdentity = UUID()
    /// Pending image attachment shown in the input area until it is sent with optional text; nil means no attachment.
    @State private var pendingImageData: Data?
    @State private var pendingMemoryUpdate: MemoryUpdatePayload?
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
    private let streamRetryBaseDelay: UInt64 = 650_000_000
    private let streamRetryMaxAttempts = 3

    /// Example-query pool from which four unique items are sampled for each empty state.
    private static let examplePool = [
        "适合油皮的洗面奶", "200 元内蓝牙耳机", "轻量跑鞋", "不要含酒精的防晒",
        "通勤双肩包推荐", "敏感肌身体乳", "适合送女友的香水", "300 元内机械键盘",
        "冬天保暖羽绒服", "学生党平价护眼台灯", "适合露营的折叠椅", "低糖代餐零食",
        "降噪头戴式耳机", "夏天透气运动短裤", "适合新手的口红色号", "家用空气炸锅"
    ]

    /// Samples four unique examples from the pool.
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

                if let pendingMemoryUpdate {
                    memoryUpdateBanner(pendingMemoryUpdate)
                        .padding(.horizontal, 16)
                        .padding(.bottom, composerBottomPadding + composerHeight + 10)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                        .zIndex(2)
                }
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
                        // Keep the active conversation title in sync so the next persistence pass does not overwrite it.
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

    private func memoryUpdateBanner(_ update: MemoryUpdatePayload) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(AppTheme.primary)
            Text(update.message)
                .font(.footnote.weight(.medium))
                .lineLimit(2)
            Spacer()
            Button("撤销") {
                Task { @MainActor in
                    await preferenceStore.undo(update)
                    pendingMemoryUpdate = nil
                }
            }
            .font(.footnote.weight(.semibold))
            .foregroundStyle(AppTheme.primary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(AppTheme.border, lineWidth: 1))
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("CartPilot")
                    .font(.custom("Georgia", size: 30).weight(.regular))
                    .tracking(0.3)
                    .scaleEffect(x: 1.06, y: 1, anchor: .leading)
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
                    // Treat content as scrollable only when it exceeds the viewport by the configured threshold.
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

    /// Empty state with a quiet greeting and simple category shortcuts, shown only before the conversation has messages.
    /// Data comes from the inventory-backed `/suggestions` endpoint and disappears after the user starts a search.
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

    /// Display categories from the backend, falling back to four defaults so the empty state is never blank.
    private var displayedCategories: [CategoryEntry] {
        let names = suggestions?.categories.isEmpty == false
            ? suggestions!.categories
            : ["数码电子", "服饰运动", "美妆护肤", "食品饮料"]
        return names.map { CategoryEntry(name: $0) }
    }

    /// Display trending searches from the backend, falling back to the local example pool while unavailable.
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

    /// Input placeholder that prompts for context when an image is attached, such as finding a similar lower-priced item.
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

    /// Places a selected follow-up suggestion in the input field for editing without sending it.
    private func fillInputWithFollowUp(_ prompt: String) {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        inputText = trimmed
        isInputFocused = true
    }

    private func sendCurrentInput() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachment = pendingImageData
        // Require either text or an image before sending.
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
        // Follow the newest content after sending so user and AI bubbles remain visible above the input area.
        isAutoFollowEnabled = true
        isNearChatBottom = true
        isUserInteractingWithChat = false
        shouldShowJumpToLatest = false
        shouldHoldLatestQuestionAnchor = false
        pendingQuestionAnchorID = nil
        let placeholder = imageData != nil
            ? "正在识别图片并匹配商品"
            : loadingSearchText(for: query)
        // Fade in the user bubble and assistant placeholder together with a spring animation.
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

    // MARK: - Visual Search

    private func openCamera() {
        isComposerExpanded = false
        guard UIImagePickerController.isSourceTypeAvailable(.camera) else {
            // The simulator has no camera, so fall back to the photo library.
            showPhotoPicker = true
            return
        }
        showCamera = true
    }

    private func openPhotoPicker() {
        isComposerExpanded = false
        showPhotoPicker = true
    }

    /// Compresses a selected or captured image, attaches it to the input area, and opens the keyboard for an optional description.
    private func handlePickedImage(_ data: Data) {
        guard let compressed = Self.compressedJPEG(from: data) else { return }
        isComposerExpanded = false
        withAnimation(.easeOut(duration: 0.2)) {
            pendingImageData = compressed
        }
        isInputFocused = true
    }

    /// Resizes the image to at most 1024 pixels on its longest edge and JPEG quality 0.7 to limit Base64 size.
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
            await runAgentWithRetry(for: query, imageBase64: imageBase64)
        }
    }

    private func runAgentWithRetry(for query: String, imageBase64: String? = nil) async {
        var attempt = 0
        var lastError: Error?

        while attempt < streamRetryMaxAttempts {
            do {
                try await runAgentOnce(for: query, imageBase64: imageBase64, retryAttempt: attempt)
                persistCurrent()
                await generateTitleIfNeeded()
                return
            } catch {
                lastError = error
                attempt += 1
                let hasProducts = latestAssistantMessage?.products.isEmpty == false
                let hasStructuredContent = latestAssistantMessage?.structuredContent != nil

                if hasProducts || hasStructuredContent {
                    markFollowupGenerationFailed("追问生成失败，正在重试")
                    try? await Task.sleep(nanoseconds: retryDelay(for: attempt))
                    completeFollowupsFromCurrentMessage(query: query)
                    persistCurrent()
                    await generateTitleIfNeeded()
                    return
                }

                markAssistantTransientFailure(attempt: attempt, maxAttempts: streamRetryMaxAttempts)
                try? await Task.sleep(nanoseconds: retryDelay(for: attempt))
            }
        }

        if latestAssistantMessage?.products.isEmpty == false || latestAssistantMessage?.structuredContent != nil {
            markFollowupGenerationFailed("追问生成失败，可以稍后重试")
        } else {
            updateLastAI(text: errorMessage(lastError ?? RESTServiceError.decodingFailed), state: .failed, canRetry: true)
        }
        persistCurrent()
    }

    private func runAgentOnce(
        for query: String,
        imageBase64: String? = nil,
        retryAttempt: Int
    ) async throws {
            var narrative = ""
            var visibleNarrative = ""
            var isStructuredNarrative = false
            var hydrated: [Product] = []
            var statusText = imageBase64 == nil ? loadingSearchText(for: query) : "正在识别图片并匹配商品"

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
                        if narrative.isEmpty && hydrated.isEmpty {
                            updateLastAI(
                                text: statusText,
                                state: status.phase.messageState,
                                products: nil
                            )
                        } else if status.phase == .generating, !hydrated.isEmpty {
                            markFollowupGenerationInProgress("生成追问中")
                        }
                    }

                case .products:
                    hydrated = event.products.map(Product.init(payload:))
                    await revealProductPreview(
                        query: query,
                        rawText: narrative.isEmpty ? statusText : narrative,
                        products: hydrated
                    )

                case .cartSnapshot:
                    if let snapshot = event.cartSnapshot {
                        syncCartItems(from: snapshot)
                    }

                case .memoryUpdate:
                    if let update = event.memoryUpdate {
                        preferenceStore.applyMemoryUpdate(update)
                        withAnimation(.easeOut(duration: 0.18)) {
                            pendingMemoryUpdate = update
                        }
                    }

                case .textDelta:
                    if let piece = event.textDelta {
                        narrative += piece
                        let trimmedNarrative = narrative.trimmingCharacters(in: .whitespacesAndNewlines)
                        if isStructuredNarrative || trimmedNarrative.first == "{" {
                            isStructuredNarrative = true
                            if hydrated.isEmpty {
                                updateLastAI(text: statusText, state: .generating, products: nil)
                            } else {
                                markFollowupGenerationInProgress("生成追问中")
                            }
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

            // Fallback for a stream that ends normally without a `done` event.
            if let index = messages.lastIndex(where: { $0.sender == .ai }),
               messages[index].state != .ready, messages[index].state != .failed {
                await finishStreamingResponse(
                    rawText: narrative.isEmpty ? statusText : narrative,
                    visibleText: visibleNarrative,
                    fallbackText: statusText,
                    products: hydrated
                )
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

    private func revealProductPreview(
        query: String,
        rawText: String,
        products: [Product]
    ) async {
        guard !products.isEmpty else { return }

        let opening = searchingRecommendationOpening(for: query)
        let previewItems = products.map {
            StructuredItem(productId: $0.id, description: $0.reason)
        }
        var visibleProducts: [Product] = []

        if latestAssistantMessage?.structuredContent == nil {
            updateLastAI(
                text: rawText,
                state: .generating,
                products: [],
                structuredContent: StructuredContent(opening: opening, items: previewItems),
                isGeneratingFollowups: true
            )
        }

        for product in products {
            guard !Task.isCancelled else { return }
            visibleProducts.append(product)
            withAnimation(.interactiveSpring(response: 0.34, dampingFraction: 0.86)) {
                updateLastAI(
                    text: rawText,
                    state: .generating,
                    products: visibleProducts,
                    structuredContent: StructuredContent(opening: opening, items: previewItems),
                    isGeneratingFollowups: true
                )
            }
            if product.id != products.last?.id {
                try? await Task.sleep(nanoseconds: streamCardRevealDelay)
            }
        }
    }

    private func revealStructuredResponse(
        _ content: StructuredContent,
        rawText: String,
        products: [Product]
    ) async {
        if latestAssistantMessage?.products.isEmpty == false {
            let stableOpening = latestAssistantMessage?.structuredContent?.opening
                ?? searchingRecommendationOpening(for: "")
            updateLastAI(
                text: rawText,
                state: .ready,
                products: nil,
                structuredContent: StructuredContent(
                    opening: stableOpening,
                    items: content.items,
                    followup: content.followup
                ),
                completionSummary: content.opening,
                isGeneratingFollowups: false,
                followupError: nil
            )
            markAssistantResponseReadyForScroll()
            return
        }

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

        updateLastAI(
            text: rawText,
            state: .ready,
            products: products,
            structuredContent: content,
            isGeneratingFollowups: false,
            followupError: nil
        )
        markAssistantResponseReadyForScroll()
    }

    private func completedRecommendationOpening(for query: String, products: [Product]) -> String {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            return "为你找到了几款和「\(trimmed)」比较匹配的商品"
        }
        if let firstTag = products.first?.tags.first, !firstTag.isEmpty {
            return "为你找到了几款\(firstTag)相关的商品"
        }
        return "为你找到了几款比较匹配的商品"
    }

    private func searchingRecommendationOpening(for query: String) -> String {
        "正在为你寻找合适的商品"
    }

    private func loadingSearchText(for query: String) -> String {
        "正在为你寻找合适的商品"
    }

    private func retryDelay(for attempt: Int) -> UInt64 {
        let multiplier = UInt64(1 << max(0, min(attempt - 1, 3)))
        return streamRetryBaseDelay * multiplier
    }

    private func markAssistantTransientFailure(attempt: Int, maxAttempts: Int) {
        guard attempt < maxAttempts else { return }
        let retryText = "连接中断，正在第 \(attempt + 1) 次重试"
        updateLastAI(text: retryText, state: .understanding, products: nil)
    }

    private func markFollowupGenerationInProgress(_ text: String) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].isGeneratingFollowups = true
        messages[index].followupError = nil
        messages[index].state = .generating
    }

    private func markFollowupGenerationFailed(_ message: String) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].isGeneratingFollowups = false
        messages[index].followupError = message
        messages[index].state = .ready
    }

    private func completeFollowupsFromCurrentMessage(query: String) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        let message = messages[index]
        let products = message.products
        let opening = message.structuredContent?.opening ?? searchingRecommendationOpening(for: query)
        let items = message.structuredContent?.items
            ?? products.map { StructuredItem(productId: $0.id, description: $0.reason) }
        let completion = completedRecommendationOpening(for: query, products: products)
        let followups = fallbackFollowups(for: query, products: products)
        messages[index].structuredContent = StructuredContent(opening: opening, items: items, followup: followups)
        messages[index].completionSummary = completion
        messages[index].isGeneratingFollowups = false
        messages[index].followupError = nil
        messages[index].state = .ready
    }

    private func fallbackFollowups(for query: String, products: [Product]) -> [String] {
        var prompts = ["推荐更便宜一点的", "对比一下前两款", "换个品牌看看"]
        if let tag = products.first?.tags.first, !tag.isEmpty {
            prompts[2] = "继续找\(tag)相关的"
        }
        if query.contains("便宜") || query.contains("平价") {
            prompts[0] = "预算再放宽一点看看"
        }
        return prompts
    }

    private func markAssistantResponseReadyForScroll() {
        shouldHoldLatestQuestionAnchor = false
        pendingQuestionAnchorID = nil
        isAutoFollowEnabled = true
        shouldShowJumpToLatest = false
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

    // MARK: - Conversation History

    /// Writes the current conversation to local history; the store ignores conversations without user messages.
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

    /// Generates a concise title with the LLM after the first exchange without blocking the conversation.
    /// Runs only while the title is still automatic so it never overwrites a user rename.
    private func generateTitleIfNeeded() async {
        // Do not regenerate a title that the LLM or user has already changed.
        guard currentTitle == GuideView.newConversationTitle
            || isAutoTruncatedTitle else { return }
        guard let firstUser = messages.first(where: { $0.sender == .user })?.text,
              !firstUser.isEmpty else { return }
        let firstAI = messages.first { $0.sender == .ai && $0.state == .ready }?.text
        let convoID = currentConversationID
        guard let title = await productService.fetchTitle(userText: firstUser, assistantText: firstAI),
              !title.isEmpty else { return }
        // The user may switch conversations while the request is in flight; update only the same conversation.
        guard convoID == currentConversationID else {
            // Update the matching conversation title directly in history.
            if var convo = store.conversation(by: convoID) {
                convo.title = title
                store.upsert(convo)
            }
            return
        }
        currentTitle = title
        persistCurrent()
    }

    /// Whether the current title is the automatic first-message excerpt that the LLM may replace.
    private var isAutoTruncatedTitle: Bool {
        guard let firstUser = messages.first(where: { $0.sender == .user })?.text else { return false }
        return currentTitle == String(firstUser.prefix(20))
    }

    /// Starts a new conversation by archiving the current one, clearing state, and requesting a new backend session ID.
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
        // Resample trending searches when returning to the empty state.
        Task { await loadSuggestions() }
    }

    /// Fetches inventory-backed category shortcuts and trending searches for the empty state.
    @MainActor
    private func loadSuggestions() async {
        if let fresh = await productService.fetchSuggestions() {
            withAnimation(.easeInOut(duration: 0.25)) {
                suggestions = fresh
            }
        }
    }

    /// Manually refreshes the trending-search suggestions.
    @MainActor
    private func refreshHotSearches() async {
        guard !isRefreshingHot else { return }
        isRefreshingHot = true
        defer { isRefreshingHot = false }
        await loadSuggestions()
    }

    /// Reopens a historical conversation after archiving the current one, preserving its session ID for follow-up questions.
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

    /// Drives scrolling when messages or status change:
    /// - Immediately after sending a short streaming reply, place the user's question at the top so the response unfolds below it.
    /// - For long replies, product cards, and ready states, follow the newest content so the input area does not obscure it.
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
        products: [Product]? = [],
        canRetry: Bool = false,
        structuredContent: StructuredContent? = nil,
        completionSummary: String? = nil,
        isGeneratingFollowups: Bool? = nil,
        followupError: String? = nil
    ) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].text = text
        messages[index].state = state
        if let products {
            messages[index].products = products
        }
        messages[index].canRetry = canRetry
        if let structuredContent {
            messages[index].structuredContent = structuredContent
        }
        if let completionSummary {
            messages[index].completionSummary = completionSummary
        }
        if let isGeneratingFollowups {
            messages[index].isGeneratingFollowups = isGeneratingFollowups
        }
        if followupError != nil || isGeneratingFollowups == false {
            messages[index].followupError = followupError
        }
    }

    /// Attaches the backend's interactive variant-selection card to the current AI message.
    /// Subsequent token and completion events update only text and status, preserving the card.
    private func attachSpecSelection(_ spec: SpecSelection) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].specSelection = spec
    }

    /// Attaches comparison results to the current AI message for inline rendering.
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
    
    private struct ProductSection: Identifiable, Equatable {
        let id: String
        let product: Product
        let description: String?
        
        init(product: Product, description: String?) {
            self.id = product.id
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
        // Prefer structured content.
        if let content = parsedStructuredContent {
            return content.opening
        }
        // Fall back to the first text paragraph.
        return textParagraphs.first
    }
    
    private var followups: [String] {
        guard message.sender == .ai else { return [] }
        if let content = parsedStructuredContent {
            return content.followup.filter {
                !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
        }
        // Fall back to text paragraphs that genuinely resemble follow-up directions.
        // Exclude the paragraph already rendered as the opening. Otherwise deterministic single-paragraph
        // copy from flows such as cart variant selection can appear both as an opening bubble and a
        // follow-up chip. An opening that is already visible must not be repeated as a follow-up.
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
        // Do not use middle paragraphs when structured content is available.
        if parsedStructuredContent != nil {
            return []
        }
        // Fall back to extracting middle paragraphs from the text.
        guard let opening = openingText else { return textParagraphs }
        var middle = textParagraphs.filter { $0 != opening }
        if !message.products.isEmpty && middle.count > message.products.count {
            middle = Array(middle.prefix(message.products.count))
        }
        return middle
    }
    
    private var productSections: [ProductSection] {
        let products = message.products
        
        // For structured content, match exact product IDs first and then align by order for LLM-generated indices.
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
        
        // Otherwise, fall back to index-based matching.
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
                    // 1. Opening message
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
                    
                    // 2. Alternating product cards and explanations
                    ForEach(productSections) { section in
                        ProductCard(product: section.product) {
                            onProductTap(section.product)
                        }
                        .transition(.opacity.combined(with: .scale).combined(with: .offset(y: 20)))

                        if let description = section.description {
                            Text(description)
                                .font(.subheadline)
                                .foregroundStyle(AppTheme.textSecondary)
                                .lineLimit(2)
                                .lineSpacing(4)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 8)
                                .transition(.opacity)
                        }
                    }

                    if let completionSummary = message.completionSummary,
                       !completionSummary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text(completionSummary)
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
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                    
                    // 3. Follow-up prompt that fills the input field without sending
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

                    if message.isGeneratingFollowups {
                        ClaudeStyleLoadingStatus(text: "生成追问中")
                            .padding(.top, 8)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    } else if let followupError = message.followupError {
                        VStack(alignment: .leading, spacing: 8) {
                            Rectangle()
                                .fill(AppTheme.border)
                                .frame(width: 148, height: 1)
                            HStack(spacing: 8) {
                                Image(systemName: "arrow.clockwise")
                                    .font(.caption.weight(.semibold))
                                Text(followupError)
                                    .font(.caption.weight(.medium))
                            }
                            .foregroundStyle(AppTheme.textSecondary)
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

                // Offer the comparison screen after a completed response recommends at least two products.
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
                // Keep the loading animation for very short text to avoid flicker on the first character.
                if message.text.count < 10 {
                    ClaudeStyleLoadingStatus(text: message.state.rawValue)
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
            ClaudeStyleLoadingStatus(text: loadingStatusText)
        }
    }

    private var loadingStatusText: String {
        let trimmed = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return message.state.rawValue }
        return trimmed.hasSuffix("中") ? trimmed : message.state.rawValue
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

private struct ClaudeStyleLoadingStatus: View {
    let text: String
    @State private var isBreathing = false
    @State private var activeDot = 0

    var body: some View {
        HStack(spacing: 8) {
            ZStack {
                Circle()
                    .fill(AppTheme.primary.opacity(0.16))
                    .frame(width: 18, height: 18)
                    .scaleEffect(isBreathing ? 1.35 : 0.72)
                    .opacity(isBreathing ? 0.20 : 0.62)

                Circle()
                    .fill(AppTheme.primary)
                    .frame(width: 7, height: 7)
                    .scaleEffect(isBreathing ? 0.88 : 1.08)
            }
            .frame(width: 18, height: 18)

            Text(normalizedText)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(AppTheme.textPrimary)

            Text("...")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(AppTheme.textPrimary)
                .opacity(ellipsisOpacity)
                .animation(.easeInOut(duration: 0.42), value: activeDot)
                .frame(width: 18, alignment: .leading)
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 2)
        .onAppear {
            withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                isBreathing = true
            }
        }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 320_000_000)
                withAnimation(.easeInOut(duration: 0.34)) {
                    activeDot = (activeDot + 1) % 3
                }
            }
        }
    }

    private var normalizedText: String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.hasSuffix("中") ? trimmed : "\(trimmed)中"
    }

    private var ellipsisOpacity: Double {
        switch activeDot {
        case 0: return 0.35
        case 1: return 0.62
        default: return 0.9
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
                ProductTagRow(tags: Array(product.tags.prefix(3)))
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

struct ProductTagRow: View {
    let tags: [String]

    var body: some View {
        HStack(spacing: 8) {
            ForEach(tags, id: \.self) { tag in
                Text(tag)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.82)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .background(AppTheme.softPurple, in: Capsule())
            }
        }
        .fixedSize(horizontal: true, vertical: false)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Interactive card for adding a multi-variant product by selecting one value per dimension.
/// On submission, the selection is converted to natural language and sent to the backend for an exact cart addition.
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

/// Display model for empty-state category shortcuts, mapping each category to an icon, tint, and query.
struct CategoryEntry: Identifiable {
    let name: String
    var id: String { name }

    /// Natural-language search query sent to the agent when the shortcut is selected.
    var query: String { "推荐\(name)" }

    var icon: String {
        switch name {
        // Use simple icons with a consistent outlined style.
        case "数码电子": return "laptopcomputer"
        case "服饰运动": return "figure.run"
        case "美妆护肤": return "drop"
        case "食品饮料", "食品生活": return "cup.and.saucer"
        default: return "bag"
        }
    }

    var tint: Color {
        switch name {
        // Use muted earth tones that complement the terracotta accent without saturated AI-style colors.
        case "数码电子": return Color(hex: "6E89A6")   // 雾蓝
        case "服饰运动": return Color(hex: "7E9B6B")   // 橄榄绿
        case "美妆护肤": return Color(hex: "C77B82")   // 豆沙粉
        case "食品饮料", "食品生活": return Color(hex: "D69A4C")   // 芥末黄
        default: return AppTheme.primary
        }
    }
}

/// Lightweight flow layout that wraps child views from left to right, used for variant chips.
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

/// Camera picker wrapping `UIImagePickerController`, because SwiftUI has no native camera entry point.
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

    /// Filters by title and message content without case sensitivity.
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
                        // Swipe left to rename or delete.
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
                        // Long press to open rename and delete actions.
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
