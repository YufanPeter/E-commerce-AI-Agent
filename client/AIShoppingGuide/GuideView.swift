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
    /// RootView 切 Tab 只改透明度，视图不会消失，语音浮层要靠它知道该关麦了。
    var isActiveTab: Bool = true
    var onOpenCart: () -> Void = {}
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
    @State private var homeFeed: HomeFeed?
    @State private var isRefreshingHot = false
    @State private var keyboardHeight: CGFloat = 0
    @State private var composerHeight: CGFloat = 56
    @State private var isAutoFollowEnabled = true
    @State private var isNearChatBottom = true
    @State private var isUserInteractingWithChat = false
    @State private var shouldShowJumpToLatest = false
    /// Whether content exceeds the viewport. The jump-to-latest control appears only when scrolling is possible and not at the bottom.
    @State private var isChatScrollable = false
    /// 对话区高度和最新问题气泡的高度，用来算最后一条回答的最小高度。
    @State private var chatViewportHeight: CGFloat = 0
    @State private var latestQuestionHeight: CGFloat = 0
    @State private var pinnedQuestionID: UUID?
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
    @State private var isVoiceOverlayPresented = false
    /// 回答刚结束时麦克风暂不响应，见 handleMicTap。
    @State private var micTapsIgnoredUntil = Date.distantPast
    /// 当前流式回合的任务句柄，停止键靠它取消请求。
    @State private var activeAgentTask: Task<Void, Never>?
    @FocusState private var isInputFocused: Bool

    private let agentService = RESTAgentService()
    private let productService = RESTProductService()
    private let nearBottomThreshold: CGFloat = 96
    private let questionAnchorCharacterLimit = 220
    private let chatListTopPadding: CGFloat = 12
    private let questionPinRetryDelays: [TimeInterval] = [0.1, 0.25, 0.4, 0.6, 0.85]
    private let autoFollowTimer = Timer.publish(every: 0.12, on: .main, in: .common).autoconnect()
    private let streamDefaultDelay: UInt64 = 16_000_000
    private let streamWhitespaceDelay: UInt64 = 6_000_000
    private let streamPunctuationDelay: UInt64 = 70_000_000
    private let streamLineBreakDelay: UInt64 = 110_000_000
    private let streamCardRevealDelay: UInt64 = 180_000_000
    /// 结构化流式：两张卡片之间至少隔这么久再弹下一张（后端解说到得再快、或命中缓存一次全到，也一张一张来）。
    private let streamCardMinGap: TimeInterval = 0.42
    /// 追问胶囊逐条出现的最小间隔。
    private let streamFollowupMinGap: TimeInterval = 0.16
    /// 开场白一次到达超过这么多字（缓存命中 / 大块 chunk）时，按逐字节奏打出来。
    private let streamOpeningTypewriterThreshold = 10
    private let streamRetryBaseDelay: UInt64 = 650_000_000
    private let streamRetryMaxAttempts = 3

    /// Example-query pool from which four unique items are sampled for each empty state.
    private static let examplePool = [
        "Face wash for oily skin", "Earbuds under $50", "Lightweight running shoes", "Facial sunscreen",
        "Coffee makers under $100", "Laptop stands", "Smartwatches, no watch bands", "Nonstick frying pans",
        "Body sunscreen", "Bedding sets", "Shampoo for dry hair", "Casual dresses"
    ]

    /// Samples four unique examples from the pool.
    private static func freshExamples() -> [String] {
        Array(examplePool.shuffled().prefix(4))
    }

    private static let newConversationTitle = "New chat"
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
                    .opacity(isVoiceOverlayPresented ? 0 : 1)
                    .allowsHitTesting(!isVoiceOverlayPresented)
                    .zIndex(1)

                if isVoiceOverlayPresented {
                    VoiceCaptureOverlay(
                        hints: voiceRecognitionHints,
                        onOutcome: handleVoiceOutcome,
                        onCancel: dismissVoiceOverlay
                    )
                    .padding(.bottom, composerBottomPadding)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                    .zIndex(3)
                }

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
            .onChange(of: isActiveTab) { _, isActive in
                if !isActive {
                    dismissVoiceOverlay()
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
            HStack(spacing: 8) {
                Button("Undo") {
                    Task { @MainActor in
                        await preferenceStore.undo(update)
                        withAnimation(.easeOut(duration: 0.18)) {
                            pendingMemoryUpdate = nil
                        }
                    }
                }
                .foregroundStyle(AppTheme.primary)

                Divider()
                    .frame(height: 16)

                Button("OK") {
                    withAnimation(.easeOut(duration: 0.18)) {
                        pendingMemoryUpdate = nil
                    }
                }
            }
            .font(.footnote.weight(.semibold))
            .foregroundStyle(AppTheme.textSecondary)
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
                Text("Shopping assistant")
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
                    Text("New chat")
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
                    .onChange(of: chatViewportHeight) { _, _ in
                        repinQuestion(proxy)
                    }
                    .onChange(of: keyboardHeight) { _, _ in
                        repinQuestion(proxy)
                    }
                    .onChange(of: latestAnswerMinHeight) { _, _ in
                        repinQuestion(proxy)
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
            // 不用 LazyVStack：发第二个问题时会把问题钉到顶部，懒加载列表跳过去后
            // 不会给新位置建行，整屏空白，要等别的状态变化才画出来。一次对话只有几十行，全部建出来没有负担。
            VStack(alignment: .leading, spacing: 16) {
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
                        onFollowUpTap: sendPrompt,
                        onSeeMore: { _ in sendPrompt("Show more") },
                        onFeedback: { submitFeedback(for: message.id, rating: $0) },
                        onAddToCart: { addToCart($0, selectedOptions: $1, quantity: $2) }
                    )
                    // 先固定成内容高度再撑最小高度，否则多余空间会被行内可伸缩的视图吃掉（比如对比表的表头）
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(minHeight: message.id == messages.last?.id ? latestAnswerMinHeight : nil, alignment: .topLeading)
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height in
                        if message.id == latestQuestionID {
                            latestQuestionHeight = height
                        }
                    }
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
            .padding(.top, chatListTopPadding)
        }
        .scrollDismissesKeyboard(.interactively)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height in
            chatViewportHeight = height
        }
        .simultaneousGesture(
            TapGesture().onEnded {
                isInputFocused = false
            }
        )
        .simultaneousGesture(
            DragGesture(minimumDistance: 8)
                .onChanged { _ in
                    isUserInteractingWithChat = true
                    shouldHoldLatestQuestionAnchor = false
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
        .accessibilityLabel("Jump to latest message")
        .accessibilityAddTraits(.isButton)
    }

    /// Empty state with a quiet greeting and simple category shortcuts, shown only before the conversation has messages.
    /// Data comes from the inventory-backed `/suggestions` endpoint and disappears after the user starts a search.
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 6) {
                HomeGreeting(text: homeGreeting)
                Text(homeFeed?.isReturning == true ? "Welcome back. What are you shopping for?" : "Tell me what you’re shopping for.")
                    .font(.subheadline)
                    .foregroundStyle(AppTheme.textSecondary)
                    .modifier(StaggeredAppear(index: 1))
            }

            if !cartItems.isEmpty || homeFeed?.entries.isEmpty == false {
                VStack(spacing: 8) {
                    if !cartItems.isEmpty {
                        homeEntry(title: "\(cartItems.count) products in your cart", imageURL: nil, action: onOpenCart)
                            .modifier(StaggeredAppear(index: 2))
                    }
                    ForEach(Array((homeFeed?.entries ?? []).enumerated()), id: \.element.id) { index, entry in
                        homeEntry(title: entry.title, imageURL: entry.imageURL) {
                            send(entry.query)
                        }
                        .modifier(StaggeredAppear(index: index + 3))
                    }
                }
            }

            VStack(alignment: .leading, spacing: 10) {
                homeSectionTitle("Browse categories")
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
            .modifier(StaggeredAppear(index: 5))

            VStack(alignment: .leading, spacing: 10) {
                homeSectionTitle("Popular searches")
                FlowLayout(spacing: 8) {
                    ForEach(Array(displayedHotSearches.prefix(6)), id: \.self) { query in
                        Button(query) { send(query) }
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.primary)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .background(AppTheme.secondary.opacity(0.7), in: Capsule())
                            .buttonStyle(.tactile)
                    }
                }
            }
            .modifier(StaggeredAppear(index: 6))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 6)
    }

    private var homeGreeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<11: return "Good morning"
        case 11..<13: return "Good afternoon"
        case 13..<18: return "Good afternoon"
        default: return "Good evening"
        }
    }

    private func homeSectionTitle(_ text: String) -> some View {
        Text(text)
            .font(.footnote.weight(.semibold))
            .foregroundStyle(AppTheme.textSecondary)
    }

    private func homeEntry(title: String, imageURL: URL?, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                if let imageURL {
                    ProductRemoteImage(url: imageURL, cornerRadius: 10, placeholderIcon: "shippingbox")
                        .frame(width: 40, height: 40)
                } else {
                    Image(systemName: "cart")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(AppTheme.primary)
                        .frame(width: 40, height: 40)
                        .background(AppTheme.secondary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
                Text(title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(AppTheme.textSecondary)
            }
            .padding(10)
            .surfacePanel(cornerRadius: 16)
        }
        .buttonStyle(.tactile)
    }

    /// 展示用分类：优先用后端返回，未就绪时退回默认四类，保证空态永不空白。
    private var displayedCategories: [CategoryEntry] {
        let names = suggestions?.categories.isEmpty == false
            ? suggestions!.categories
            : ["Electronics", "Clothing & Sports", "Beauty & Personal Care", "Home & Kitchen"]
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
                            ComposerAction(icon: "camera.fill", title: "Camera") { openCamera() }
                            ComposerAction(icon: "photo.fill", title: "Photo library") { openPhotoPicker() }
                        }
                    }
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        ComposerAction(icon: "camera.fill", title: "Camera") { openCamera() }
                        ComposerAction(icon: "photo.fill", title: "Photo library") { openPhotoPicker() }
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

                composerTrailingButtons
            }
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .floatingLiquidPanel(cornerRadius: 24)
        }
        .onChange(of: isAssistantStreaming) { wasStreaming, isStreaming in
            if wasStreaming && !isStreaming {
                micTapsIgnoredUntil = Date().addingTimeInterval(0.7)
            }
        }
    }

    /// 输入栏右侧按钮：有内容就能发送，没内容时可以说话；生成中多一个停止键。
    /// 生成中说话或打字都是在改需求，发出去才会打断当前回答。
    /// 只挂了图片时麦克风和发送都在，用户可以直接发图，也可以先说一句补充要求。
    @ViewBuilder
    private var composerTrailingButtons: some View {
        let hasText = !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if !hasText {
            Button(action: handleMicTap) {
                Image(systemName: "mic.fill")
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(AppTheme.textSecondary)
                    .frame(width: 38, height: 38)
            }
            .buttonStyle(.tactile)
            .accessibilityLabel("Voice input")
        }

        if hasComposerPayload {
            Button(action: sendCurrentInput) {
                Image(systemName: "arrow.up")
                    .font(.system(size: 17, weight: .bold))
                    .foregroundStyle(.white)
                    .frame(width: 38, height: 38)
                    .background(AppTheme.primary, in: Circle())
            }
            .buttonStyle(.tactile)
            .accessibilityLabel(isAssistantStreaming ? "Update request and continue" : "Send")
        } else if isAssistantStreaming {
            Button(action: { stopGeneration() }) {
                Image(systemName: "stop.fill")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(AppTheme.background)
                    .frame(width: 38, height: 38)
                    .background(AppTheme.textPrimary, in: Circle())
            }
            .buttonStyle(.tactile)
            .accessibilityLabel("Stop response")
        }
    }

    private var hasComposerPayload: Bool {
        !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || pendingImageData != nil
    }

    /// Input placeholder that prompts for context when an image is attached, such as finding a similar lower-priced item.
    private var composerPlaceholder: String {
        if pendingImageData != nil {
            return "Add a detail, such as a lower budget or the same brand"
        }
        return isAssistantStreaming ? "Want to change your request?" : "Search products, brands, or needs"
    }

    private var isKeyboardPresented: Bool {
        keyboardHeight > 0
    }

    private var composerBottomPadding: CGFloat {
        isKeyboardPresented ? 10 : AppTheme.guideComposerBottomPadding
    }

    private var latestQuestionID: UUID? {
        messages.last(where: { $0.sender == .user })?.id
    }

    /// 让最新的问题总能滚到顶部：内容不够一屏时，由最后一条回答撑足剩余高度。
    /// 否则问题要等回答长到足够高才滚得上去，回答一开始页面就会猛跳一下。
    private var latestAnswerMinHeight: CGFloat? {
        guard messages.count >= 2,
              messages.last?.sender == .ai,
              chatViewportHeight > 0 else { return nil }
        // 问题到回答之间、回答到底部留白之间各有列表间距和 1pt 尾部锚点
        let rowGaps: CGFloat = 16 * 4 + 2
        let remaining = chatViewportHeight - latestQuestionHeight - rowGaps - chatListBottomPadding
        return max(0, remaining)
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
    /// 点追问或"Show more"：等于用户发了这句话，生成中会先停掉当前回答。
    private func sendPrompt(_ prompt: String) {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        inputText = trimmed
        sendCurrentInput()
    }

    private func sendCurrentInput() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachment = pendingImageData
        // 至少要有文字或图片其一才发送；生成中发新消息是在改需求，后端会接上被打断的问题
        guard !trimmed.isEmpty || attachment != nil else { return }
        let isAmending = isAssistantStreaming
        stopGeneration(isAmending: isAmending)
        guard canStartSend(query: trimmed, imageData: attachment) else { return }
        inputText = ""
        pendingImageData = nil
        send(trimmed, imageData: attachment, isAmending: isAmending)
    }

    private func send(_ query: String, imageData: Data? = nil, isAmending: Bool = false) {
        guard canStartSend(query: query, imageData: imageData) else { return }
        isComposerExpanded = false
        isInputFocused = false
        lastQuery = query
        if currentTitle == GuideView.newConversationTitle {
            currentTitle = imageData != nil && query.isEmpty
                ? "Search with a photo"
                : String(query.prefix(20))
        }
        let userMessage = ChatMessage(sender: .user, text: query, localImageData: imageData)
        pendingAutoFollowWorkItem?.cancel()
        // Alexa 式阅读顺序：把这条问题钉在顶部，回答在下方一段段长出来；
        // 不跟随滚到底部，生成完也不往下跳，用户从上往下读。
        isAutoFollowEnabled = false
        isNearChatBottom = true
        isUserInteractingWithChat = false
        shouldShowJumpToLatest = false
        shouldHoldLatestQuestionAnchor = true
        pendingQuestionAnchorID = userMessage.id
        let placeholder = imageData != nil
            ? "Matching products from the photo…"
            : loadingSearchText(for: query)
        // Fade in the user bubble and assistant placeholder together with a spring animation.
        withAnimation(.spring(response: 0.38, dampingFraction: 0.82)) {
            messages.append(userMessage)
            messages.append(ChatMessage(
                sender: .ai,
                text: placeholder,
                state: .understanding,
                thinkingSteps: [imageData != nil ? "Reading the photo…" : (isAmending ? Self.amendThinkingStep : "Thinking…")]
            ))
        }
        runAgent(for: query, imageBase64: imageData?.base64EncodedString())
    }

    // MARK: - 语音输入

    /// 把真实库存里的类目和热搜词交给识别器，提高品牌名、品类词的命中率。
    private var voiceRecognitionHints: [String] {
        guard let suggestions else { return [] }
        return suggestions.categories + suggestions.hotSearches
    }

    /// 停止键和麦克风在同一位置。回答恰好结束时，用户想点停止却会点到刚换上来的麦克风，
    /// 所以按钮切换后短暂忽略点击。
    private func handleMicTap() {
        guard Date() >= micTapsIgnoredUntil else { return }
        presentVoiceOverlay()
    }

    private func presentVoiceOverlay() {
        isInputFocused = false
        isComposerExpanded = false
        withAnimation(.spring(response: 0.34, dampingFraction: 0.86)) {
            isVoiceOverlayPresented = true
        }
    }

    private func dismissVoiceOverlay() {
        guard isVoiceOverlayPresented else { return }
        withAnimation(.easeOut(duration: 0.2)) {
            isVoiceOverlayPresented = false
        }
    }

    private func handleVoiceOutcome(_ outcome: VoiceCaptureOutcome) {
        dismissVoiceOverlay()
        switch outcome {
        case .send(let text):
            inputText = text
            sendCurrentInput()
        case .draft(let text):
            inputText = text
            isInputFocused = true
        case .noSpeech:
            break
        }
    }

    // MARK: - 停止生成

    private static let amendThinkingStep = "Updating your request…"

    /// 取消当前回合并保留用户已经看到的内容。断开连接后，后端会关闭 LLM 流并记录半截回合。
    /// isAmending：用户是在改需求。还没内容的回答直接去掉，有内容的标明新结果在下面。
    private func stopGeneration(isAmending: Bool = false) {
        guard isAssistantStreaming else { return }
        activeAgentTask?.cancel()
        activeAgentTask = nil
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }

        var message = messages[index]
        let placeholderOpening = searchingRecommendationOpening(for: lastQuery)
        let hasCards = !message.products.isEmpty || message.comparison != nil || message.specSelection != nil
        let hasRealOpening = message.structuredContent.map {
            !$0.opening.isEmpty && $0.opening != placeholderOpening
        } ?? false
        // 思考步骤还在，说明回答一个字都没开始，这时的 text 只是阶段提示
        let hasRealText = message.structuredContent == nil
            && message.state == .generating
            && !message.text.isEmpty
            && (message.thinkingSteps ?? []).isEmpty

        if isAmending, !hasCards, !hasRealOpening, !hasRealText {
            withAnimation(.easeOut(duration: 0.18)) {
                _ = messages.remove(at: index)
            }
            return
        }

        if hasCards || hasRealOpening || hasRealText {
            message.wasStopped = true
            message.wasAmended = isAmending ? true : nil
            if !hasRealOpening, let content = message.structuredContent {
                let opening = completedRecommendationOpening(for: lastQuery, products: message.products)
                message.text = opening
                message.structuredContent = StructuredContent(
                    opening: opening,
                    items: content.items,
                    followup: content.followup,
                    summary: content.summary,
                    points: content.points,
                    picks: content.picks
                )
            }
            // 表已经出来但回答还没开始，这时的 text 只是阶段提示
            if !hasRealOpening, !hasRealText, message.structuredContent == nil {
                message.text = ""
            }
        } else {
            message.text = "Response stopped"
            message.structuredContent = nil
        }
        message.state = .ready
        message.canRetry = false
        message.streamingStatusText = nil
        message.isGeneratingFollowups = false
        message.followupError = nil
        message.thinkingSteps = nil
        withAnimation(.easeOut(duration: 0.18)) {
            messages[index] = message
        }
        persistCurrent()
        refreshCartAfterStop()
    }

    /// 用户可能在加购工具已执行后才点停止，这时收不到购物车快照，稍后从后端对一次账。
    private func refreshCartAfterStop() {
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            if let snapshot = try? await productService.fetchAgentCart() {
                syncCartItems(from: snapshot)
            }
        }
    }

    // MARK: - 回答反馈

    private func submitFeedback(for messageID: UUID, rating: String) {
        guard let index = messages.firstIndex(where: { $0.id == messageID }) else { return }
        let previous = messages[index].feedback
        guard previous != rating else { return }
        withAnimation(.easeOut(duration: 0.18)) {
            messages[index].feedback = rating
        }
        persistCurrent()
        let query = messages[..<index].last(where: { $0.sender == .user })?.text ?? ""
        let answer = messages[index].shareText
        let sessionID = currentSessionID
        Task { @MainActor in
            let succeeded = await productService.submitFeedback(
                messageID: messageID.uuidString,
                rating: rating,
                sessionID: sessionID,
                query: query,
                answer: answer
            )
            guard !succeeded, let current = messages.firstIndex(where: { $0.id == messageID }) else { return }
            withAnimation(.easeOut(duration: 0.18)) {
                messages[current].feedback = previous
            }
            persistCurrent()
        }
    }

    private func retryLast() {
        guard !isAssistantStreaming else { return }
        messages.removeAll { $0.canRetry }
        let query = lastQuery.isEmpty ? "Search again" : lastQuery
        pendingAutoFollowWorkItem?.cancel()
        isAutoFollowEnabled = true
        shouldHoldLatestQuestionAnchor = false
        pendingQuestionAnchorID = nil
        shouldShowJumpToLatest = false
        messages.append(ChatMessage(
            sender: .ai,
            text: "Searching again…",
            state: .understanding,
            thinkingSteps: ["Thinking…"]
        ))
        runAgent(for: query)
    }

    private func canStartSend(query: String, imageData: Data?) -> Bool {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return (!trimmed.isEmpty || imageData != nil) && !isAssistantStreaming
    }

    // MARK: - 拍照找货

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
        activeAgentTask = Task { @MainActor in
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
                // 用户点了停止：界面已由 stopGeneration 收尾，不算失败也不重试
                if Task.isCancelled { return }
                lastError = error
                attempt += 1
                let hasProducts = latestAssistantMessage?.products.isEmpty == false
                let hasStructuredContent = latestAssistantMessage?.structuredContent != nil

                if hasProducts || hasStructuredContent {
                    markFollowupGenerationFailed("Could not generate suggestions. Retrying…")
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
            markFollowupGenerationFailed("Could not generate suggestions. Try again later.")
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
            var statusText = imageBase64 == nil ? loadingSearchText(for: query) : "Matching products from the photo…"

            // 结构化流式（新协议）：后端按"开场白逐字 → 每款商品解说 → 追问"顺序下发，
            // 卡片随自己的解说到达时才弹出，而不是检索一完成就整批砸出来。
            var hasStructuredStream = false
            var streamedOpening = ""
            var streamedItems: [StructuredItem] = []
            var streamedFollowups: [String] = []
            var streamedSummary: String?
            var streamedPoints: [String] = []
            var streamedPicks: [String] = []
            // 商品已补全但还没收到任何结构化片段：先只摆开场占位，等解说到了再逐张弹卡片。
            // 若后端是旧协议（只发 token），在首个 token / done 时退回旧的整批预览。
            var pendingProductPreview = false
            // 节奏控制：上一张卡片 / 上一条追问弹出的时刻，保证按固定节奏一个一个来
            var lastCardRevealAt = Date.distantPast
            var lastFollowupRevealAt = Date.distantPast

            let request = AgentRequestPayload(
                sessionID: currentSessionID,
                text: query,
                imageBase64: imageBase64
            )
            for try await event in agentService.streamResponse(for: request) {
                try Task.checkCancellation()
                switch event.type {
                case .status:
                    if let status = event.status {
                        statusText = status.message.isEmpty ? statusText : status.message
                        appendThinkingStep(status.message)
                        if hasStructuredStream {
                            break
                        }
                        if narrative.isEmpty && hydrated.isEmpty {
                            updateLastAI(
                                text: statusText,
                                state: status.phase.messageState,
                                products: nil
                            )
                        } else if status.phase == .generating, !hydrated.isEmpty {
                            markFollowupGenerationInProgress("Preparing follow-up suggestions…")
                        }
                    }

                case .products:
                    hydrated = event.products.map { payload in
                        var product = Product(payload: payload)
                        product.budgetHint = event.budgets[payload.productID]
                        return product
                    }
                    if hasStructuredStream {
                        renderStructuredStream(
                            query: query,
                            opening: streamedOpening,
                            items: streamedItems,
                            followups: streamedFollowups,
                            summary: streamedSummary,
                            points: streamedPoints,
                            picks: streamedPicks,
                            hydrated: hydrated
                        )
                    } else {
                        // 卡片等解说到了再逐张弹出，这段时间继续显示思考步骤
                        pendingProductPreview = true
                    }

                case .openingDelta:
                    if let piece = event.textDelta {
                        hasStructuredStream = true
                        pendingProductPreview = false
                        setStreamingStatus(hydrated.isEmpty ? "Preparing recommendations…" : "Preparing product details…")
                        if piece.count >= streamOpeningTypewriterThreshold {
                            // 一大块一次到（缓存命中）：按逐字节奏打出来，别"啪"地整句出现
                            for character in piece {
                                guard !Task.isCancelled else { break }
                                streamedOpening.append(character)
                                renderStructuredStream(
                                    query: query,
                                    opening: streamedOpening,
                                    items: streamedItems,
                                    followups: streamedFollowups,
                                    summary: streamedSummary,
                                    points: streamedPoints,
                                    picks: streamedPicks,
                                    hydrated: hydrated
                                )
                                try? await Task.sleep(nanoseconds: streamDelay(for: character))
                            }
                        } else {
                            streamedOpening += piece
                            renderStructuredStream(
                                query: query,
                                opening: streamedOpening,
                                items: streamedItems,
                                followups: streamedFollowups,
                                summary: streamedSummary,
                                points: streamedPoints,
                                picks: streamedPicks,
                                hydrated: hydrated
                            )
                        }
                    }

                case .itemDescription:
                    if let pid = event.productID {
                        hasStructuredStream = true
                        pendingProductPreview = false
                        let description = event.textDelta ?? ""
                        guard !streamedItems.contains(where: { $0.productId == pid }) else { break }
                        // 一张一张来：距上一张不足最小间隔就等一等（后续事件在流里排队，不会丢）
                        await paceReveal(since: lastCardRevealAt, minGap: streamCardMinGap)
                        try Task.checkCancellation()
                        lastCardRevealAt = Date()
                        streamedItems.append(StructuredItem(productId: pid, description: description, group: event.group))
                        let remaining = hydrated.count - streamedItems.count
                        setStreamingStatus(remaining > 0 ? "Preparing product details…" : "Preparing follow-up suggestions…")
                        withAnimation(.interactiveSpring(response: 0.34, dampingFraction: 0.86)) {
                            renderStructuredStream(
                                query: query,
                                opening: streamedOpening,
                                items: streamedItems,
                                followups: streamedFollowups,
                                summary: streamedSummary,
                                points: streamedPoints,
                                picks: streamedPicks,
                                hydrated: hydrated
                            )
                        }
                    }

                case .summary:
                    if let text = event.textDelta {
                        hasStructuredStream = true
                        pendingProductPreview = false
                        streamedSummary = text
                        withAnimation(.easeOut(duration: 0.18)) {
                            renderStructuredStream(
                                query: query,
                                opening: streamedOpening,
                                items: streamedItems,
                                followups: streamedFollowups,
                                summary: streamedSummary,
                                points: streamedPoints,
                                picks: streamedPicks,
                                hydrated: hydrated
                            )
                        }
                    }

                case .comparePoint, .comparePick:
                    if let text = event.textDelta {
                        hasStructuredStream = true
                        pendingProductPreview = false
                        await paceReveal(since: lastFollowupRevealAt, minGap: streamFollowupMinGap)
                        try Task.checkCancellation()
                        lastFollowupRevealAt = Date()
                        if event.type == .comparePoint {
                            streamedPoints.append(text)
                        } else {
                            streamedPicks.append(text)
                        }
                        withAnimation(.easeOut(duration: 0.18)) {
                            renderStructuredStream(
                                query: query,
                                opening: streamedOpening,
                                items: streamedItems,
                                followups: streamedFollowups,
                                summary: streamedSummary,
                                points: streamedPoints,
                                picks: streamedPicks,
                                hydrated: hydrated
                            )
                        }
                    }

                case .followup:
                    if let text = event.textDelta, !streamedFollowups.contains(text) {
                        hasStructuredStream = true
                        pendingProductPreview = false
                        await paceReveal(since: lastFollowupRevealAt, minGap: streamFollowupMinGap)
                        try Task.checkCancellation()
                        lastFollowupRevealAt = Date()
                        streamedFollowups.append(text)
                        setStreamingStatus("Preparing follow-up suggestions…")
                        withAnimation(.easeOut(duration: 0.18)) {
                            renderStructuredStream(
                                query: query,
                                opening: streamedOpening,
                                items: streamedItems,
                                followups: streamedFollowups,
                                summary: streamedSummary,
                                points: streamedPoints,
                                picks: streamedPicks,
                                hydrated: hydrated
                            )
                        }
                    }

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
                        if hasStructuredStream {
                            // 结构化片段已经渲染过，这条是后端为旧客户端附带的完整 JSON，仅留作 rawText。
                            break
                        }
                        if pendingProductPreview {
                            // 旧协议后端：没有结构化片段，退回整批预览
                            pendingProductPreview = false
                            await revealProductPreview(query: query, rawText: statusText, products: hydrated)
                        }
                        let trimmedNarrative = narrative.trimmingCharacters(in: .whitespacesAndNewlines)
                        if isStructuredNarrative || trimmedNarrative.first == "{" {
                            isStructuredNarrative = true
                            if hydrated.isEmpty {
                                updateLastAI(text: statusText, state: .generating, products: nil)
                            } else {
                                markFollowupGenerationInProgress("Preparing follow-up suggestions…")
                            }
                        } else {
                            clearThinkingSteps()
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
                    if hasStructuredStream {
                        finishStructuredStream(
                            query: query,
                            rawText: narrative,
                            opening: streamedOpening,
                            items: streamedItems,
                            followups: streamedFollowups,
                            summary: streamedSummary,
                            points: streamedPoints,
                            picks: streamedPicks,
                            hydrated: hydrated
                        )
                    } else {
                        if pendingProductPreview {
                            pendingProductPreview = false
                            await revealProductPreview(query: query, rawText: statusText, products: hydrated)
                        }
                        await finishStreamingResponse(
                            rawText: narrative.isEmpty ? statusText : narrative,
                            visibleText: visibleNarrative,
                            fallbackText: statusText,
                            products: hydrated
                        )
                    }

                default:
                    break
                }
            }

            // 被取消时 for-await 会正常退出，不能走下面"没收到 done"的兜底
            try Task.checkCancellation()

            // 流正常结束但未收到 done 时的兜底
            if let index = messages.lastIndex(where: { $0.sender == .ai }),
               messages[index].state != .ready, messages[index].state != .failed {
                if hasStructuredStream {
                    finishStructuredStream(
                        query: query,
                        rawText: narrative,
                        opening: streamedOpening,
                        items: streamedItems,
                        followups: streamedFollowups,
                        summary: streamedSummary,
                        points: streamedPoints,
                        picks: streamedPicks,
                        hydrated: hydrated
                    )
                } else {
                    if pendingProductPreview {
                        await revealProductPreview(query: query, rawText: statusText, products: hydrated)
                    }
                    await finishStreamingResponse(
                        rawText: narrative.isEmpty ? statusText : narrative,
                        visibleText: visibleNarrative,
                        fallbackText: statusText,
                        products: hydrated
                    )
                }
            }
    }

    // MARK: - 结构化流式渲染（开场白 → 卡片逐张 → 追问）

    /// 距上次弹出不足 minGap 就等到够为止；流里后到的事件会排队，不会丢。
    private func paceReveal(since last: Date, minGap: TimeInterval) async {
        let elapsed = Date().timeIntervalSince(last)
        guard elapsed < minGap else { return }
        try? await Task.sleep(nanoseconds: UInt64((minGap - elapsed) * 1_000_000_000))
    }

    /// 流式进行中显示在当前 AI 消息底部的「生成中」文案；nil 则隐藏。
    private func setStreamingStatus(_ text: String?) {
        guard !Task.isCancelled else { return }
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        if messages[index].streamingStatusText != text {
            messages[index].streamingStatusText = text
        }
    }

    /// 把后端的阶段提示记成一条思考步骤，已经显示过的不重复。
    private func appendThinkingStep(_ text: String) {
        guard !Task.isCancelled else { return }
        guard let index = messages.lastIndex(where: { $0.sender == .ai }),
              messages[index].structuredContent == nil else { return }
        let step = Self.thinkingStepText(text)
        // 本地先放的第一步带省略号（"Thinking…"），要去掉省略号再比，否则后端的同一步会再显示一遍
        let last = messages[index].thinkingSteps?.last.map(Self.thinkingStepText)
        guard !step.isEmpty, last != step else { return }
        // 改需求时第一步已经是"Updating your request…"，后端的"Thinking…"不用再显示一遍
        if step == Self.thinkingStepText("Thinking…"), last == Self.thinkingStepText(Self.amendThinkingStep) { return }
        withAnimation(.easeOut(duration: 0.2)) {
            messages[index].thinkingSteps = (messages[index].thinkingSteps ?? []) + [step]
        }
    }

    /// 步骤文字去掉首尾空白和结尾的省略号；正在进行的那一步由时间线自己加省略号。
    fileprivate static func thinkingStepText(_ text: String) -> String {
        text
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "…."))
    }

    private func clearThinkingSteps() {
        guard !Task.isCancelled else { return }
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].thinkingSteps = nil
    }

    /// 按"解说到达顺序"渲染：只有拿到专属解说的商品才出现卡片。
    private func renderStructuredStream(
        query: String,
        opening: String,
        items: [StructuredItem],
        followups: [String],
        summary: String?,
        points: [String] = [],
        picks: [String] = [],
        hydrated: [Product]
    ) {
        let byID = Dictionary(hydrated.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        let visibleProducts = items.compactMap { byID[$0.productId] }
        let openingText = opening.isEmpty ? searchingRecommendationOpening(for: query) : opening
        updateLastAI(
            text: openingText,
            state: .generating,
            products: visibleProducts,
            structuredContent: StructuredContent(
                opening: openingText,
                items: items,
                followup: followups,
                summary: summary,
                points: points.isEmpty ? nil : points,
                picks: picks.isEmpty ? nil : picks
            ),
            isGeneratingFollowups: followups.isEmpty
        )
    }

    /// 流结束：补齐没拿到解说的商品（用检索理由兜底）、补默认追问，切到 ready。
    private func finishStructuredStream(
        query: String,
        rawText: String,
        opening: String,
        items: [StructuredItem],
        followups: [String],
        summary: String?,
        points: [String] = [],
        picks: [String] = [],
        hydrated: [Product]
    ) {
        let byID = Dictionary(hydrated.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        var orderedProducts = items.compactMap { byID[$0.productId] }
        var finalItems = items
        for product in hydrated where !orderedProducts.contains(where: { $0.id == product.id }) {
            orderedProducts.append(product)
            finalItems.append(StructuredItem(productId: product.id, description: product.reason))
        }
        let openingText = opening.isEmpty
            ? completedRecommendationOpening(for: query, products: orderedProducts)
            : opening
        let finalFollowups = followups.isEmpty && !orderedProducts.isEmpty
            ? fallbackFollowups(for: query, products: orderedProducts)
            : followups
        let content = StructuredContent(
            opening: openingText,
            items: finalItems,
            followup: finalFollowups,
            summary: summary,
            points: points.isEmpty ? nil : points,
            picks: picks.isEmpty ? nil : picks
        )
        setStreamingStatus(nil)
        withAnimation(.easeOut(duration: 0.18)) {
            updateLastAI(
                text: rawText.isEmpty ? openingText : rawText,
                state: .ready,
                products: orderedProducts,
                structuredContent: content,
                isGeneratingFollowups: false,
                followupError: nil
            )
        }
        markAssistantResponseReadyForScroll()
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
                    followup: content.followup,
                    summary: content.summary
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
                    structuredContent: StructuredContent(opening: content.opening, items: content.items, summary: content.summary)
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
            return "Here are some options for “\(trimmed).”"
        }
        if let firstTag = products.first?.tags.first, !firstTag.isEmpty {
            return "Here are some options in \(firstTag)."
        }
        return "Here are some options for your search."
    }

    private func searchingRecommendationOpening(for query: String) -> String {
        "Looking for matching products…"
    }

    private func loadingSearchText(for query: String) -> String {
        "Looking for matching products…"
    }

    private func retryDelay(for attempt: Int) -> UInt64 {
        let multiplier = UInt64(1 << max(0, min(attempt - 1, 3)))
        return streamRetryBaseDelay * multiplier
    }

    private func markAssistantTransientFailure(attempt: Int, maxAttempts: Int) {
        guard attempt < maxAttempts else { return }
        let retryText = "Connection lost. Retry \(attempt + 1)…"
        updateLastAI(text: retryText, state: .understanding, products: nil)
        if let index = messages.lastIndex(where: { $0.sender == .ai }) {
            messages[index].thinkingSteps = [retryText]
        }
    }

    private func markFollowupGenerationInProgress(_ text: String) {
        guard !Task.isCancelled else { return }
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].isGeneratingFollowups = true
        messages[index].followupError = nil
        messages[index].state = .generating
    }

    private func markFollowupGenerationFailed(_ message: String) {
        guard !Task.isCancelled else { return }
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
        messages[index].structuredContent = StructuredContent(
            opening: opening,
            items: items,
            followup: followups,
            summary: message.structuredContent?.summary,
            points: message.structuredContent?.points,
            picks: message.structuredContent?.picks
        )
        messages[index].completionSummary = completion
        messages[index].isGeneratingFollowups = false
        messages[index].followupError = nil
        messages[index].state = .ready
    }

    private func fallbackFollowups(for query: String, products: [Product]) -> [String] {
        var prompts = ["Show cheaper options", "Compare the first and second", "Show other brands"]
        if let tag = products.first?.tags.first, !tag.isEmpty {
            prompts[2] = "Show more \(tag)"
        }
        if query.contains("cheap") || query.contains("affordable") {
            prompts[0] = "Clear the budget"
        }
        return prompts
    }

    /// 回答完成：松开问题锚点但不往下跳；内容超出一屏时给「跳到最新」按钮。
    private func markAssistantResponseReadyForScroll() {
        shouldHoldLatestQuestionAnchor = false
        pendingQuestionAnchorID = nil
        isAutoFollowEnabled = false
        shouldShowJumpToLatest = !isNearChatBottom
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
        stopGeneration()
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
        if let feed = await productService.fetchHomeFeed() {
            withAnimation(.easeOut(duration: 0.25)) {
                homeFeed = feed
            }
        }
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
        stopGeneration()
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
            message += "Invalid service URL\n\n"
            errorMessage = "The product service URL is invalid."
        case .invalidResponse:
            message += "Invalid service response\n\n"
            errorMessage = "The product service returned an invalid response."
        case .requestFailed(let code, _, let msg):
            errorMessage = msg
            switch code {
            case 400:
                message += "Invalid request\n\n"
            case 401, 403:
                message += "Access was denied\n\n"
            case 404:
                message += "Product not found\n\n"
            case 429:
                message += "Too many requests. Try again shortly.\n\n"
            case 500...599:
                message += "The server is unavailable\n\n"
            default:
                message += "Product service unavailable\n\n"
            }
        case .connectionFailed:
            message += "Could not connect\n\n"
            errorMessage = "The product service is unavailable. Make sure the backend is running."
        case .decodingFailed:
            message += "Could not read the response\n\n"
            errorMessage = "Could not read the product service response."
        }
        
        message += "Reason: \(errorMessage)\n\n"
        
        message += "What you can try:\n"
        message += "• Check your connection\n"
        message += "• Try a simpler search\n"
        message += "• Try again in a moment\n"
        
        return message
    }
    
    private func defaultErrorMessage() -> String {
        return "Product service unavailable\n\n" +
               "Reason: Unknown error\n\n" +
               "What you can try:\n" +
               "• Check your connection\n" +
               "• Try a simpler search\n" +
               "• Tap the button below to retry"
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
        if shouldHoldLatestQuestionAnchor, let anchorID = pendingQuestionAnchorID {
            // 问题钉在顶部，回答在下方长出来；超出一屏只提示「跳到最新」，不自动滚
            scrollToQuestion(anchorID, proxy: proxy)
            if !isNearChatBottom {
                shouldShowJumpToLatest = true
            }
            return
        }
        if let message = latestAssistantMessage, message.state == .ready || message.state == .failed {
            if isAutoFollowEnabled {
                forceScrollToLatestMessage(proxy)
            }
            return
        }
        handleContentChange(proxy)
    }

    /// 把问题钉到顶部。不能带动画：懒加载列表在动画事务里重建上一条回答时，
    /// 它的卡片会从估算位置飞过，叠在新内容上闪一下。
    private func scrollToQuestion(_ id: UUID, proxy: ScrollViewProxy) {
        pendingAutoFollowWorkItem?.cancel()
        DispatchQueue.main.async {
            proxy.scrollTo(id, anchor: questionPinAnchor)
        }
        // 键盘收起是动画，期间底部留白和最小高度都在变，定位会被覆盖，动画结束后也不会有新的触发。
        // 所以发送后的短时间内多钉几次；已经到位时不会有任何视觉变化，用户一拖就停。
        guard pinnedQuestionID != id else { return }
        pinnedQuestionID = id
        for delay in questionPinRetryDelays {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                repinQuestion(proxy)
            }
        }
    }

    /// 键盘收起时列表高度一路在变，刚才的定位会失效。高度一变就重新钉住，
    /// 否则偏差要留到下一个思考步骤才纠正，看起来就是页面跳了一下。
    /// 问题停在列表顶部留白之下，和第一轮不滚动时的位置一致，每轮都停在同一处。
    private var questionPinAnchor: UnitPoint {
        let freeHeight = chatViewportHeight - latestQuestionHeight
        guard freeHeight > 0 else { return .top }
        return UnitPoint(x: 0.5, y: chatListTopPadding / freeHeight)
    }

    private func repinQuestion(_ proxy: ScrollViewProxy) {
        guard shouldHoldLatestQuestionAnchor, let id = pendingQuestionAnchorID else { return }
        proxy.scrollTo(id, anchor: questionPinAnchor)
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
        // 已取消的回合可能刚从 sleep 醒来，不能覆盖 stopGeneration 的收尾或下一轮的消息
        guard !Task.isCancelled else { return }
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].text = text
        messages[index].state = state
        if state == .ready || state == .failed {
            messages[index].thinkingSteps = nil
        }
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
        guard !Task.isCancelled else { return }
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].specSelection = spec
    }

    /// Attaches comparison results to the current AI message for inline rendering.
    private func attachComparison(_ comparison: ProductComparisonPayload) {
        guard !Task.isCancelled else { return }
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
    var onSeeMore: (String) -> Void = { _ in }
    var onFeedback: (String) -> Void = { _ in }
    var onAddToCart: (Product, [String: String], Int) -> Void = { _, _, _ in }
    
    private struct ProductSection: Identifiable, Equatable {
        let id: String
        let product: Product
        let description: String?
        /// 这款商品是所属分区的第一款时，在卡片上方显示分区标题。
        var groupHeader: String? = nil
        
        init(product: Product, description: String?) {
            self.id = product.id
            self.product = product
            self.description = description
        }
        
        static func == (lhs: ProductSection, rhs: ProductSection) -> Bool {
            lhs.product.id == rhs.product.id
                && lhs.description == rhs.description
                && lhs.groupHeader == rhs.groupHeader
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
                paragraph.contains("need") || paragraph.contains("want") ||
                paragraph.contains("looking for")
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
            var currentGroup: String?
            return products.enumerated().map { index, product in
                let item = content.items.first { $0.productId == product.id }
                    ?? (index < content.items.count ? content.items[index] : nil)
                let description = item.flatMap {
                    let cleaned = RecommendationCopy.sanitized($0.description)
                    return cleaned.isEmpty ? nil : cleaned
                } ?? (product.reason.isEmpty ? nil : product.reason)
                var section = ProductSection(product: product, description: description)
                if let group = item?.group, !group.isEmpty, group != currentGroup {
                    section.groupHeader = group
                    currentGroup = group
                }
                return section
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

                if let steps = activeThinkingSteps {
                    ThinkingStepsView(steps: steps)
                        .transition(.opacity)
                } else if message.state == .ready || isRevealingStructuredContent {
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
                    
                    if let comparison = message.comparison {
                        ComparisonCard(
                            comparison: comparison,
                            revealsRows: isRevealingStructuredContent,
                            showsRecommendation: (parsedStructuredContent?.picks ?? []).isEmpty
                        )
                        .transition(.opacity)
                    }

                    if let points = parsedStructuredContent?.points, !points.isEmpty {
                        ComparisonInsightList(title: "What the differences mean", items: points)
                            .transition(.opacity)
                    }

                    if let picks = parsedStructuredContent?.picks, !picks.isEmpty {
                        ComparisonInsightList(title: "How to choose", items: picks)
                            .transition(.opacity)
                    }

                    // 2. 商品卡片 + 解说交替展示
                    ForEach(productSections) { section in
                        if let header = section.groupHeader {
                            ProductSectionHeader(title: header) {
                                onSeeMore(header)
                            }
                            .modifier(GhostReveal(isActive: isRevealingStructuredContent))
                        }

                        ProductCard(
                            product: section.product,
                            onDetail: { onProductTap(section.product) },
                            onAddToCart: onAddToCart
                        )
                        .modifier(GhostReveal(isActive: isRevealingStructuredContent))
                        .transition(.offset(y: 16))

                        if let description = section.description {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(description)
                                    .font(.subheadline)
                                    .foregroundStyle(AppTheme.textPrimary)
                                    .lineSpacing(4)
                                    .fixedSize(horizontal: false, vertical: true)
                                Button("View details") {
                                    onProductTap(section.product)
                                }
                                .font(.footnote.weight(.medium))
                                .foregroundStyle(AppTheme.primary)
                                .buttonStyle(.tactile)
                            }
                            .padding(.horizontal, 10)
                            .padding(.bottom, 6)
                            .modifier(GhostReveal(isActive: isRevealingStructuredContent))
                        }
                    }

                    if let closingSummary {
                        Text(highlightingProductNames(closingSummary))
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
                    
                    // 3. 追问 Prompt（点击直接发送）
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

                    if message.streamingStatusText != nil || message.isGeneratingFollowups {
                        // 流式进行中：统一一个加载动画，不区分「介绍商品 / 生成追问」
                        StreamingPulseIndicator()
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

                if message.wasStopped == true {
                    Label(
                        message.wasAmended == true ? "Request updated. New results below." : "Stopped",
                        systemImage: message.wasAmended == true ? "arrow.down.circle" : "stop.circle"
                    )
                        .font(.caption.weight(.medium))
                        .foregroundStyle(AppTheme.textSecondary)
                        .padding(.horizontal, 4)
                        .transition(.opacity)
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
                                Text("Try again")
                                    .font(.headline.weight(.semibold))
                            }
                            .padding(.horizontal, 24)
                            .padding(.vertical, 14)
                            .background(AppTheme.primary)
                            .foregroundColor(.white)
                            .cornerRadius(14)
                            .shadow(color: AppTheme.primary.opacity(0.22), radius: 6, x: 0, y: 3)
                        }
                        
                        Text("Tap above to search again")
                            .font(.caption)
                            .foregroundStyle(AppTheme.textSecondary)
                    }
                    .padding(.top, 4)
                }

                if let spec = message.specSelection {
                    SpecSelectionCard(selection: spec, onSubmit: onSpecSubmit)
                }

                // 对话结束、推荐了 ≥2 件商品时，给个小按钮进对比页（页内下拉选商品）。
                if message.sender == .ai, message.state == .ready, message.products.count >= 2 {
                    Button {
                        onCompareTap(message.products)
                    } label: {
                        Label("Compare products", systemImage: "square.split.2x1")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(AppTheme.primary)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                            .background(AppTheme.softPurple, in: Capsule())
                    }
                    .buttonStyle(.tactile)
                }

                if showsFeedbackBar {
                    AnswerFeedbackBar(
                        rating: message.feedback,
                        shareText: message.shareText,
                        onRate: onFeedback
                    )
                    .padding(.top, 2)
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
                        Text("Something went wrong")
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
        return trimmed.isEmpty ? message.state.rawValue : trimmed
    }

    private var displayText: String {
        switch message.state {
        case .ready, .failed:
            return message.text
        default:
            return message.text
        }
    }

    /// 真正给出了内容的回答才能评价和分享，出错和"Response stopped"不算。
    private var showsFeedbackBar: Bool {
        message.sender == .ai
            && message.state == .ready
            && !message.canRetry
            && message.followupError == nil
            && !message.text.isEmpty
            && message.text != "Response stopped"
    }

    /// 回答开始前显示思考步骤；一旦有开场白或正文就不再显示。
    private var activeThinkingSteps: [String]? {
        guard message.sender == .ai,
              message.state != .ready,
              message.state != .failed,
              message.structuredContent == nil,
              let steps = message.thinkingSteps,
              !steps.isEmpty else { return nil }
        return steps
    }

    /// 模型写的结尾总结优先；没有时用本地兜底生成的完成说明。
    private var closingSummary: String? {
        let candidate = parsedStructuredContent?.summary ?? message.completionSummary
        guard let candidate,
              !candidate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return candidate
    }

    private var isRevealingStructuredContent: Bool {
        message.sender == .ai && message.state == .generating && message.structuredContent != nil
    }

    private var shouldShowStreamingPlainText: Bool {
        guard message.sender == .ai, message.state == .generating else { return false }
        let trimmed = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        guard message.structuredContent == nil else { return false }
        return !trimmed.hasPrefix("Looking for matching products")
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

/// 流式进行中挂在消息底部的加载动画：三个依次起伏的圆点，不带文字。
private struct StreamingPulseIndicator: View {
    @State private var phase = 0

    var body: some View {
        HStack(spacing: 6) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(AppTheme.primary.opacity(phase == index ? 0.95 : 0.35))
                    .frame(width: 7, height: 7)
                    .scaleEffect(phase == index ? 1.25 : 0.85)
                    .animation(.easeInOut(duration: 0.3), value: phase)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(AppTheme.surface, in: Capsule())
        .overlay(Capsule().stroke(AppTheme.border, lineWidth: 1))
        .accessibilityLabel("Writing…")
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 300_000_000)
                phase = (phase + 1) % 3
            }
        }
    }
}

/// 首页问候：逐字出现。
private struct HomeGreeting: View {
    let text: String

    @State private var revealed = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        // 隐藏的完整文字先占好高度，逐字出现时下面的内容不会跳
        Text(text)
            .font(.system(size: 26, weight: .semibold))
            .hidden()
            .overlay(alignment: .leading) {
                Text(String(text.prefix(reduceMotion ? text.count : revealed)))
                    .font(.system(size: 26, weight: .semibold))
                    .foregroundStyle(AppTheme.textPrimary)
            }
            .accessibilityLabel(text)
            .task(id: text) {
                guard !reduceMotion else { return }
                revealed = 0
                for count in 1...max(text.count, 1) {
                    guard !Task.isCancelled else { return }
                    revealed = count
                    try? await Task.sleep(nanoseconds: 70_000_000)
                }
            }
    }
}

/// 首页内容依次淡入。
private struct StaggeredAppear: ViewModifier {
    let index: Int

    @State private var isVisible = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(isVisible || reduceMotion ? 1 : 0)
            .offset(y: isVisible || reduceMotion ? 0 : 8)
            .onAppear {
                guard !isVisible else { return }
                withAnimation(.easeOut(duration: 0.3).delay(0.12 + Double(index) * 0.07)) {
                    isVisible = true
                }
            }
    }
}

/// 对比表下面的解读：差异意味着什么、怎么选。"维度：说明"里的维度加粗。
private struct ComparisonInsightList: View {
    let title: String
    let items: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.textPrimary)
            ForEach(items, id: \.self) { item in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Circle()
                        .fill(AppTheme.primary)
                        .frame(width: 5, height: 5)
                        .alignmentGuide(.firstTextBaseline) { $0[.bottom] }
                    styledText(item)
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineSpacing(4)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .transition(.opacity)
            }
        }
        .padding(.horizontal, 4)
    }

    private func styledText(_ item: String) -> Text {
        guard let colon = item.firstIndex(of: "："), item.distance(from: item.startIndex, to: colon) <= 8 else {
            return Text(highlightingProductNames(item))
        }
        return Text(item[...colon]).fontWeight(.semibold) + Text(highlightingProductNames(String(item[item.index(after: colon)...])))
    }
}

/// 模型在总结和"怎么选"里用 **名字** 标出推荐的商品，这里把它们画成品牌色加粗，一眼看出推荐了谁。
/// 星号不成对时，落单的那段当普通文字。
func highlightingProductNames(_ text: String) -> AttributedString {
    let parts = text.components(separatedBy: "**")
    var result = AttributedString()
    for (index, part) in parts.enumerated() {
        var piece = AttributedString(part)
        let isMarked = !index.isMultiple(of: 2) && index < parts.count - 1
        if isMarked {
            piece.font = .subheadline.weight(.semibold)
            piece.foregroundColor = AppTheme.primary
        }
        result += piece
    }
    return result
}

/// 回答下方的点赞、点踩和分享。
private struct AnswerFeedbackBar: View {
    let rating: String?
    let shareText: String
    let onRate: (String) -> Void

    var body: some View {
        HStack(spacing: 2) {
            rateButton(value: "up", symbol: "hand.thumbsup", label: "Helpful")
            rateButton(value: "down", symbol: "hand.thumbsdown", label: "Not helpful")
            ShareLink(item: shareText) {
                Image(systemName: "square.and.arrow.up")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(AppTheme.textSecondary)
                    .frame(width: 40, height: 40)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("Share")
            if rating != nil {
                Text("Feedback sent")
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .padding(.leading, 6)
                    .transition(.opacity)
            }
        }
    }

    private func rateButton(value: String, symbol: String, label: String) -> some View {
        let isSelected = rating == value
        return Button {
            onRate(value)
        } label: {
            Image(systemName: isSelected ? "\(symbol).fill" : symbol)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(isSelected ? AppTheme.primary : AppTheme.textSecondary)
                .frame(width: 40, height: 40)
                .contentShape(Rectangle())
        }
        .buttonStyle(.tactile)
        .accessibilityLabel(label)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

/// 思考步骤时间线：做完的变灰，正在做的高亮并呼吸。
private struct ThinkingStepsView: View {
    let steps: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                let isActive = index == steps.count - 1
                // 存下来的步骤可能自带省略号，统一去掉，只给正在进行的那一步加
                let label = GuideView.thinkingStepText(step)
                HStack(alignment: .firstTextBaseline, spacing: 9) {
                    ThinkingStepDot(isActive: isActive)
                    Text(isActive ? "\(label)…" : label)
                        .font(.footnote.weight(isActive ? .medium : .regular))
                        .foregroundStyle(isActive ? AppTheme.textPrimary : AppTheme.textSecondary.opacity(0.7))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .transition(.opacity.combined(with: .offset(y: 6)))
            }
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

private struct ThinkingStepDot: View {
    let isActive: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 20, paused: !isActive || reduceMotion)) { context in
            let wave = sin(context.date.timeIntervalSinceReferenceDate * 3.4)
            Circle()
                .fill(isActive ? AppTheme.primary : AppTheme.textSecondary.opacity(0.45))
                .frame(width: 6, height: 6)
                .scaleEffect(isActive && !reduceMotion ? 1 + 0.22 * wave : 1)
                .opacity(isActive && !reduceMotion ? 0.72 + 0.28 * wave : 1)
        }
        .frame(width: 8, height: 8)
    }
}

/// 分区标题：左边说明这组的共同点，右边"Show more"继续看同类、不重复已展示的。
private struct ProductSectionHeader: View {
    let title: String
    let onSeeMore: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            Button("Show more", action: onSeeMore)
                .font(.footnote.weight(.medium))
                .foregroundStyle(AppTheme.primary)
                .buttonStyle(.tactile)
        }
        .padding(.horizontal, 4)
        .padding(.top, 6)
    }
}

/// 流式到达的内容先以半透明的幽灵态出现，再慢慢变实。历史消息和已完成的回答直接显示。
private struct GhostReveal: ViewModifier {
    let isActive: Bool

    @State private var isRevealed = false

    func body(content: Content) -> some View {
        content
            .opacity(isActive && !isRevealed ? 0.25 : 1)
            .saturation(isActive && !isRevealed ? 0.2 : 1)
            .onAppear {
                guard isActive, !isRevealed else { return }
                withAnimation(.easeOut(duration: 0.55)) {
                    isRevealed = true
                }
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
        return trimmed
    }

    private var ellipsisOpacity: Double {
        switch activeDot {
        case 0: return 0.35
        case 1: return 0.62
        default: return 0.9
        }
    }

}

/// 一次语音输入的结果。
enum VoiceCaptureOutcome: Equatable {
    /// 说完了，直接发送。
    case send(String)
    /// 像是说到一半，放回输入框让用户补完，不直接发送也不丢弃。
    case draft(String)
    /// 一直没有说话，收起浮层。
    case noSpeech
}

/// 判断用户是否说完。纯逻辑，不依赖音频，时间由调用方传入。
struct VoiceEndpointDetector {
    enum Decision: Equatable {
        case keepListening, send, draft, noSpeech
    }

    /// 识别结果多久没变算说完。
    var silenceToSend: TimeInterval = 1.6
    /// 句子像没说完时多等一会儿，用户可能在想。
    var silenceToDraft: TimeInterval = 3.5
    /// 麦克风还要安静这么久才算停，避免识别结果滞后时打断用户。
    var voiceQuietGap: TimeInterval = 0.8
    /// 环境噪音会让麦克风一直不安静，超过这个额外时长就不再看音量。
    var noisyRoomExtra: TimeInterval = 2.4
    /// 一个字都没识别到时的等待上限。
    var noSpeechTimeout: TimeInterval = 8
    /// 音量高于它算有人在说话。
    var voicedLevel: CGFloat = 0.18

    private let startedAt: Date
    private var lastTranscriptChangeAt: Date
    private var lastVoicedAt: Date

    init(startedAt: Date) {
        self.startedAt = startedAt
        self.lastTranscriptChangeAt = startedAt
        self.lastVoicedAt = startedAt
    }

    mutating func noteTranscriptChanged(at now: Date) {
        lastTranscriptChangeAt = now
    }

    mutating func noteLevel(_ level: CGFloat, at now: Date) {
        if level >= voicedLevel {
            lastVoicedAt = now
        }
    }

    func decide(transcript: String, now: Date) -> Decision {
        let text = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            return now.timeIntervalSince(startedAt) >= noSpeechTimeout ? .noSpeech : .keepListening
        }
        let incomplete = Self.looksIncomplete(text)
        let required = incomplete ? silenceToDraft : silenceToSend
        let sinceTranscript = now.timeIntervalSince(lastTranscriptChangeAt)
        let isQuiet = now.timeIntervalSince(lastVoicedAt) >= voiceQuietGap
        guard (sinceTranscript >= required && isQuiet) || sinceTranscript >= required + noisyRoomExtra else {
            return .keepListening
        }
        return incomplete ? .draft : .send
    }

    /// 以连接词、语气词结尾的话大概率没说完。
    static func looksIncomplete(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
        guard !trimmed.isEmpty else { return false }
        let englishEndings: Set<String> = ["of", "and", "the", "to", "with", "for", "a", "an", "or", "but", "want", "need", "um", "uh"]
        let lastWord = trimmed.lowercased().split(whereSeparator: { !$0.isLetter }).last.map(String.init)
        return lastWord.map(englishEndings.contains) ?? false
    }
}

/// 转写太长时只留结尾，超出部分用省略号代替。中文按两个单位算，英文按一个。
func voiceTranscriptTail(_ text: String, maxUnits: Int = 108) -> String {
    var units = 0
    var kept: [Character] = []
    for character in text.reversed() {
        let width = character.isASCII ? 1 : 2
        if units + width > maxUnits {
            return "…" + String(kept.reversed())
        }
        units += width
        kept.append(character)
    }
    return text
}

@MainActor
final class SpeechInputController: NSObject, ObservableObject, SFSpeechRecognizerDelegate {
    @Published private(set) var isListening = false
    @Published private(set) var transcript = ""
    /// 0...1 的麦克风音量，驱动音量条。
    @Published private(set) var level: CGFloat = 0
    @Published private(set) var errorMessage: String?
    /// 一次语音输入结束后写入，只写一次。
    @Published private(set) var outcome: VoiceCaptureOutcome?

    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "en_US"))
    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var endpointTask: Task<Void, Never>?
    private var detector = VoiceEndpointDetector(startedAt: Date())

    override init() {
        super.init()
        speechRecognizer?.delegate = self
    }

    func start(hints: [String] = []) async {
        errorMessage = nil
        outcome = nil
        guard speechRecognizer?.isAvailable == true else {
            errorMessage = "Speech recognition is unavailable."
            return
        }
        guard await requestSpeechAuthorization() else {
            errorMessage = "Allow speech recognition to use voice input."
            return
        }
        guard await requestMicrophoneAuthorization() else {
            errorMessage = "Allow microphone access to use voice input."
            return
        }
        // 用户可能在权限弹框期间已经取消了
        guard !Task.isCancelled else { return }

        recognitionTask?.cancel()
        recognitionTask = nil
        transcript = ""

        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.record, mode: .measurement, options: .duckOthers)
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)

            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            request.addsPunctuation = true
            request.contextualStrings = Array(hints.prefix(100))
            recognitionRequest = request

            let inputNode = audioEngine.inputNode
            inputNode.removeTap(onBus: 0)
            let recordingFormat = inputNode.outputFormat(forBus: 0)
            // 没有可用麦克风时格式为空，直接 installTap 会让进程崩溃
            guard recordingFormat.sampleRate > 0, recordingFormat.channelCount > 0 else {
                stop()
                errorMessage = "No microphone was detected."
                return
            }
            let tap = Self.makeTapBlock(request: request) { [weak self] value in
                Task { @MainActor in
                    self?.applyLevel(value)
                }
            }
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat, block: tap)

            audioEngine.prepare()
            try audioEngine.start()
            isListening = true
            detector = VoiceEndpointDetector(startedAt: Date())
            startEndpointLoop()

            recognitionTask = speechRecognizer?.recognitionTask(with: request) { [weak self] result, error in
                Task { @MainActor in
                    guard let self, self.isListening else { return }
                    if let result {
                        self.updateTranscript(result.bestTranscription.formattedString)
                        if result.isFinal {
                            self.finishWithCurrentTranscript()
                            return
                        }
                    }
                    if error != nil {
                        self.handleRecognitionFailure()
                    }
                }
            }
        } catch {
            stop()
            errorMessage = "Could not start voice input. Try again."
        }
    }

    /// 用户点音量条表示说完了，不再等静音判断。
    func finishNow() {
        guard isListening, !currentText.isEmpty else { return }
        finish(.send(currentText))
    }

    func stop() {
        endpointTask?.cancel()
        endpointTask = nil
        if audioEngine.isRunning {
            audioEngine.stop()
        }
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionRequest = nil
        recognitionTask = nil
        isListening = false
        level = 0
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private var currentText: String {
        transcript.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func updateTranscript(_ text: String) {
        guard text != transcript else { return }
        transcript = text
        detector.noteTranscriptChanged(at: Date())
    }

    private func applyLevel(_ value: CGFloat) {
        guard isListening else { return }
        // 上升跟得快、回落慢一点，音量条才不会抖
        level = max(value, level * 0.82)
        detector.noteLevel(value, at: Date())
    }

    private func startEndpointLoop() {
        endpointTask?.cancel()
        endpointTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 200_000_000)
                guard !Task.isCancelled, let self else { return }
                self.evaluateEndpoint()
            }
        }
    }

    private func evaluateEndpoint() {
        guard isListening else { return }
        switch detector.decide(transcript: transcript, now: Date()) {
        case .keepListening:
            break
        case .send:
            finish(.send(currentText))
        case .draft:
            finish(.draft(currentText))
        case .noSpeech:
            finish(.noSpeech)
        }
    }

    private func finishWithCurrentTranscript() {
        finish(currentText.isEmpty ? .noSpeech : .send(currentText))
    }

    /// 识别中断时已经听到的话不能丢，放回输入框。
    private func handleRecognitionFailure() {
        finish(currentText.isEmpty ? .noSpeech : .draft(currentText))
    }

    private func finish(_ result: VoiceCaptureOutcome) {
        guard outcome == nil else { return }
        stop()
        outcome = result
    }

    /// 音频线程上运行，所以不能继承 MainActor 隔离。
    private nonisolated static func makeTapBlock(
        request: SFSpeechAudioBufferRecognitionRequest,
        onLevel: @escaping @Sendable (CGFloat) -> Void
    ) -> AVAudioNodeTapBlock {
        { [weak request] buffer, _ in
            request?.append(buffer)
            onLevel(normalizedLevel(of: buffer))
        }
    }

    /// 把这段音频的响度换算成 0...1，-50dB 以下算静音，-12dB 以上算满格。
    private nonisolated static func normalizedLevel(of buffer: AVAudioPCMBuffer) -> CGFloat {
        guard let samples = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return 0 }
        let count = Int(buffer.frameLength)
        var sum: Float = 0
        for index in 0..<count {
            sum += samples[index] * samples[index]
        }
        let rms = (sum / Float(count)).squareRoot()
        guard rms > 0 else { return 0 }
        let decibels = 20 * log10(rms)
        return CGFloat(min(1, max(0, (decibels + 50) / 38)))
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
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    nonisolated func speechRecognizer(
        _ speechRecognizer: SFSpeechRecognizer,
        availabilityDidChange available: Bool
    ) {
        Task { @MainActor in
            if !available {
                errorMessage = "Speech recognition is unavailable."
                stop()
            }
        }
    }
}

/// 语音输入浮层：盖住输入栏，对话内容仍然可见。实时转写在上，音量条在下。
/// 控制器由浮层自己持有，音量每秒刷新几十次也不会让整个对话页重绘。
private struct VoiceCaptureOverlay: View {
    let hints: [String]
    let onOutcome: (VoiceCaptureOutcome) -> Void
    let onCancel: () -> Void

    @StateObject private var speech = SpeechInputController()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(spacing: 14) {
            promptText
                .multilineTextAlignment(.center)
                .lineLimit(3)
                .lineSpacing(3)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 24)
                .animation(.easeOut(duration: 0.12), value: speech.transcript)

            VoiceWaveform(level: speech.level)
                .contentShape(Rectangle())
                .onTapGesture { speech.finishNow() }
                .accessibilityElement()
                .accessibilityLabel("Listening")
                .accessibilityHint("Tap to finish speaking and send")
                .accessibilityAddTraits(.isButton)

            Button("Cancel", action: onCancel)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(AppTheme.textSecondary)
                .frame(minWidth: 88, minHeight: 36)
                .contentShape(Rectangle())
        }
        .padding(.top, 48)
        .padding(.bottom, 4)
        .frame(maxWidth: .infinity)
        .background(alignment: .bottom) {
            // 渐隐只占顶部留白，转写文字下面始终是实底，长句也不会和商品卡片叠在一起
            VStack(spacing: 0) {
                LinearGradient(
                    colors: [AppTheme.background.opacity(0), AppTheme.background],
                    startPoint: .top,
                    endPoint: .bottom
                )
                .frame(height: 40)
                AppTheme.background
            }
            .padding(.bottom, -AppTheme.guideComposerBottomPadding)
            .ignoresSafeArea(edges: .bottom)
        }
        .task {
            await speech.start(hints: hints)
        }
        .onChange(of: speech.outcome) { _, outcome in
            if let outcome {
                onOutcome(outcome)
            }
        }
        // 只看 background：系统权限弹框会让 scenePhase 变成 inactive，不能因此收起
        .onChange(of: scenePhase) { _, phase in
            if phase == .background {
                onCancel()
            }
        }
        // 进商品详情页时视图会消失，要同步收起，否则返回时会重新开麦
        .onDisappear {
            speech.stop()
            onCancel()
        }
    }

    @ViewBuilder
    private var promptText: some View {
        if let error = speech.errorMessage {
            Text(error)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(AppTheme.error)
        } else if speech.transcript.isEmpty {
            Text("Start speaking")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
        } else {
            Text(voiceTranscriptTail(speech.transcript))
                .font(.body.weight(.bold))
                .foregroundStyle(AppTheme.textPrimary)
        }
    }
}

/// 五根音量条。没声音时收成五个圆点，有声音时中间高两边低。
private struct VoiceWaveform: View {
    let level: CGFloat

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let weights: [CGFloat] = [0.55, 0.8, 1, 0.8, 0.55]
    private let barWidth: CGFloat = 8
    private let maxBarHeight: CGFloat = 38

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
            let time = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 5) {
                ForEach(weights.indices, id: \.self) { index in
                    Capsule()
                        .fill(AppTheme.primary)
                        .frame(width: barWidth, height: height(of: index, at: time))
                }
            }
            .frame(height: maxBarHeight)
        }
    }

    private func height(of index: Int, at time: TimeInterval) -> CGFloat {
        let wobble = reduceMotion ? 1 : 0.72 + 0.28 * CGFloat(sin(time * 9 + Double(index) * 1.4))
        let fraction = min(1, level * weights[index] * wobble)
        return barWidth + (maxBarHeight - barWidth) * fraction
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

/// 回答流里的紧凑商品卡：左图右文，一屏能看到多款。点卡片进详情，点按钮直接加购。
struct ProductCard: View {
    let product: Product
    let onDetail: () -> Void
    let onAddToCart: (Product, [String: String], Int) -> Void

    @State private var selectedOptions: [String: String]
    @State private var quantity = 1
    @State private var showSpecSheet = false
    @State private var justAdded = false

    init(
        product: Product,
        onDetail: @escaping () -> Void,
        onAddToCart: @escaping (Product, [String: String], Int) -> Void
    ) {
        self.product = product
        self.onDetail = onDetail
        self.onAddToCart = onAddToCart
        _selectedOptions = State(initialValue: product.defaultSpecificationSelection)
    }

    private var needsSpecChoice: Bool {
        product.specifications.contains { $0.options.count > 1 }
    }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            ProductRemoteImage(url: product.imageURL, cornerRadius: 12, placeholderIcon: "shippingbox", contentMode: .fit)
                .frame(width: 108, height: 108)
                .background(Color.white, in: RoundedRectangle(cornerRadius: 12, style: .continuous))

            VStack(alignment: .leading, spacing: 5) {
                Text(product.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                tagLine

                // 多规格显示完整区间，避免「¥480 起」让用户以为看中的规格也是这个价
                Text(product.priceRangeDisplay)
                    .font(.headline.bold())
                    .foregroundStyle(AppTheme.error)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)

                if let caption = product.budgetHint?.caption {
                    Text(caption)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(AppTheme.textSecondary)
                        .lineLimit(1)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(AppTheme.secondary.opacity(0.6), in: Capsule())
                }

                if product.referenceOnly == true {
                    Text("Availability unknown")
                        .font(.caption2)
                        .foregroundStyle(AppTheme.textSecondary)
                } else {
                    addButton.padding(.top, 2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(10)
        .surfacePanel(cornerRadius: 16)
        .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .onTapGesture(perform: onDetail)
        .accessibilityAction(named: "View details", onDetail)
        .sheet(isPresented: $showSpecSheet) {
            AddToCartSheet(
                product: product,
                selectedOptions: $selectedOptions,
                quantity: $quantity,
                onConfirm: {
                    showSpecSheet = false
                    commitAdd()
                }
            )
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
            .presentationCornerRadius(28)
        }
    }

    @ViewBuilder
    private var tagLine: some View {
        if !product.tags.isEmpty {
            Text(product.tags.prefix(2).joined(separator: " · "))
                .font(.caption)
                .foregroundStyle(AppTheme.textSecondary)
                .lineLimit(1)
        }
    }

    private var addButton: some View {
        Button {
            if needsSpecChoice {
                showSpecSheet = true
            } else {
                commitAdd()
            }
        } label: {
            HStack(spacing: 4) {
                if justAdded {
                    Image(systemName: "checkmark")
                        .font(.caption2.weight(.bold))
                }
                Text(justAdded ? "Added" : (needsSpecChoice ? "Choose options" : "Add to cart"))
            }
            .font(.caption.weight(.semibold))
            .foregroundStyle(justAdded ? AppTheme.success : .white)
            .padding(.horizontal, 14)
            .frame(height: 30)
            .background(justAdded ? AppTheme.success.opacity(0.14) : AppTheme.primary, in: Capsule())
        }
        .buttonStyle(.tactile)
        .disabled(justAdded)
    }

    private func commitAdd() {
        onAddToCart(product, selectedOptions, quantity)
        quantity = 1
        withAnimation(.easeOut(duration: 0.2)) { justAdded = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) {
            withAnimation(.easeOut(duration: 0.2)) { justAdded = false }
        }
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
        if submitted { return "Selected" }
        return allChosen ? "Add to cart" : "Choose options"
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
        let phrase = "I want " + selection.dimensions
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
    var query: String { "Browse \(name)" }

    var icon: String {
        switch name {
        // Use simple icons with a consistent outlined style.
        case "Electronics": return "laptopcomputer"
        case "Clothing & Sports": return "figure.run"
        case "Beauty & Personal Care": return "drop"
        case "Food & Drinks", "Home & Kitchen": return "cup.and.saucer"
        default: return "bag"
        }
    }

    var tint: Color {
        switch name {
        // Use muted earth tones that complement the terracotta accent without saturated AI-style colors.
        case "Electronics": return Color(hex: "6E89A6")   // 雾蓝
        case "Clothing & Sports": return Color(hex: "7E9B6B")   // 橄榄绿
        case "Beauty & Personal Care": return Color(hex: "C77B82")   // 豆沙粉
        case "Food & Drinks", "Home & Kitchen": return Color(hex: "D69A4C")   // 芥末黄
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
                Text("Chat history")
                    .font(.title3.bold())
                Spacer()
                if !conversations.isEmpty {
                    Button(role: .destructive) {
                        showClearAllConfirm = true
                    } label: {
                        Label("Clear", systemImage: "trash")
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
                                Label("Delete", systemImage: "trash")
                            }
                            Button {
                                startRename(conversation)
                            } label: {
                                Label("Rename", systemImage: "pencil")
                            }
                            .tint(AppTheme.primary)
                        }
                        // Long press to open rename and delete actions.
                        .contextMenu {
                            Button {
                                startRename(conversation)
                            } label: {
                                Label("Rename", systemImage: "pencil")
                            }
                            Button(role: .destructive) {
                                pendingDelete = conversation
                            } label: {
                                Label("Delete chat", systemImage: "trash")
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
            "Delete this chat?",
            isPresented: Binding(
                get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Delete", role: .destructive) {
                if let target = pendingDelete { onDelete(target) }
                pendingDelete = nil
            }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: {
            Text(pendingDelete?.title ?? "")
        }
        .alert("Rename chat", isPresented: Binding(
            get: { renameTarget != nil },
            set: { if !$0 { renameTarget = nil } }
        )) {
            TextField("Chat title", text: $renameText)
            Button("Save") {
                if let target = renameTarget {
                    onRename(target, renameText)
                }
                renameTarget = nil
            }
            Button("Cancel", role: .cancel) { renameTarget = nil }
        }
        .confirmationDialog(
            "Clear all chats? This cannot be undone.",
            isPresented: $showClearAllConfirm,
            titleVisibility: .visible
        ) {
            Button("Clear all \(conversations.count) chats", role: .destructive) { onClearAll() }
            Button("Cancel", role: .cancel) {}
        }
    }

    private var searchBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
            TextField("Search chat history", text: $searchText)
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
            Text("No previous chats")
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
            Text("No matching chats")
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
#Preview("Recommendations") {
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

#Preview("Loading") {
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
