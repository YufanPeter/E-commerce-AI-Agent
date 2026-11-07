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
    @ObservedObject var preferenceStore: PreferenceStore
    /// RootView 切 Tab 只改透明度，视图不会消失，语音浮层要靠它知道该关麦了。
    var isActiveTab: Bool = true
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
    @State private var pendingMemoryUpdate: MemoryUpdatePayload?
    @State private var isVoiceOverlayPresented = false
    @State private var didConsiderVoiceAutoStart = false
    /// 回答刚结束时麦克风暂不响应，见 handleMicTap。
    @State private var micTapsIgnoredUntil = Date.distantPast
    /// 当前流式回合的任务句柄，停止键靠它取消请求。
    @State private var activeAgentTask: Task<Void, Never>?
    /// 最近一次写进助手消息的阶段提示；停止时用来区分提示文案和真实回答。
    @State private var lastStreamingStatusText: String?
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
    /// 结构化流式：两张卡片之间至少隔这么久再弹下一张（后端解说到得再快、或命中缓存一次全到，也一张一张来）。
    private let streamCardMinGap: TimeInterval = 0.42
    /// 追问胶囊逐条出现的最小间隔。
    private let streamFollowupMinGap: TimeInterval = 0.16
    /// 开场白一次到达超过这么多字（缓存命中 / 大块 chunk）时，按逐字节奏打出来。
    private let streamOpeningTypewriterThreshold = 10
    private let streamRetryBaseDelay: UInt64 = 650_000_000
    private let streamRetryMaxAttempts = 3

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
                autoStartVoiceIfAppropriate()
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
                Button("撤销") {
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

                Button("确定") {
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
            HStack(spacing: 8) {
                Image("BrandMark")
                    .resizable()
                    .scaledToFit()
                    .frame(height: 40)
                Image("BrandWordmark")
                    .renderingMode(.template)
                    .resizable()
                    .scaledToFit()
                    .frame(height: 30)
                    .foregroundStyle(AppTheme.textPrimary)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("兜兜")
            .accessibilityAddTraits(.isHeader)
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
        .accessibilityLabel("跳到最新消息")
        .accessibilityAddTraits(.isButton)
    }

    /// 空态：一句安静的问候 + 朴素的分类入口。仅在当前会话还没有任何消息时显示，
    /// 数据来自后端 /suggestions（真实库存），点哪条都一定有结果；用户发起检索后随消息出现而隐去。
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Hi，我是兜兜")
                    .font(.system(size: 26, weight: .bold))
                    .foregroundStyle(AppTheme.textPrimary)
                Text("想要什么直接说，或选个分类")
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

    /// 输入栏右侧按钮：有内容就能发送，生成中且没内容时是停止键，空闲时是麦克风。
    /// 只挂了图片时麦克风和发送都在，用户可以直接发图，也可以先说一句补充要求。
    @ViewBuilder
    private var composerTrailingButtons: some View {
        let hasText = !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if !hasText && !isAssistantStreaming {
            Button(action: handleMicTap) {
                Image(systemName: "mic.fill")
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(AppTheme.textSecondary)
                    .frame(width: 38, height: 38)
            }
            .buttonStyle(.tactile)
            .accessibilityLabel("语音输入")
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
            .accessibilityLabel(isAssistantStreaming ? "停止并发送" : "发送")
        } else if isAssistantStreaming {
            Button(action: stopGeneration) {
                Image(systemName: "stop.fill")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(AppTheme.background)
                    .frame(width: 38, height: 38)
                    .background(AppTheme.textPrimary, in: Circle())
            }
            .buttonStyle(.tactile)
            .accessibilityLabel("停止生成")
        }
    }

    private var hasComposerPayload: Bool {
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
        // 至少要有文字或图片其一才发送；生成中发新消息等于先停掉当前回答
        guard !trimmed.isEmpty || attachment != nil else { return }
        stopGeneration()
        guard canStartSend(query: trimmed, imageData: attachment) else { return }
        inputText = ""
        pendingImageData = nil
        send(trimmed, imageData: attachment)
    }

    private func send(_ query: String, imageData: Data? = nil) {
        guard canStartSend(query: query, imageData: imageData) else { return }
        isComposerExpanded = false
        lastQuery = query
        if currentTitle == GuideView.newConversationTitle {
            currentTitle = imageData != nil && query.isEmpty
                ? "拍照找货"
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
            ? "正在识别图片并匹配商品"
            : loadingSearchText(for: query)
        // 用户气泡 + 助手占位一起以弹性动画淡入，避免"啪"地直接出现。
        withAnimation(.spring(response: 0.38, dampingFraction: 0.82)) {
            messages.append(userMessage)
            messages.append(ChatMessage(sender: .ai, text: placeholder, state: .understanding))
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

    /// 启动时停在空对话页就直接开麦。只在用户已授权时才开，不会一启动就弹系统权限框。
    private func autoStartVoiceIfAppropriate() {
        guard !didConsiderVoiceAutoStart else { return }
        didConsiderVoiceAutoStart = true
        guard isActiveTab,
              messages.isEmpty,
              inputText.isEmpty,
              !isInputFocused,
              SpeechInputController.isAuthorizedWithoutPrompt else { return }
        presentVoiceOverlay()
    }

    // MARK: - 停止生成

    /// 取消当前回合并保留用户已经看到的内容。断开连接后，后端会关闭 LLM 流并记录半截回合。
    private func stopGeneration() {
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
        let hasRealText = message.structuredContent == nil
            && message.state == .generating
            && !message.text.isEmpty
            && message.text != lastStreamingStatusText

        if hasCards || hasRealOpening || hasRealText {
            message.wasStopped = true
            if !hasRealOpening, let content = message.structuredContent {
                let opening = completedRecommendationOpening(for: lastQuery, products: message.products)
                message.text = opening
                message.structuredContent = StructuredContent(
                    opening: opening,
                    items: content.items,
                    followup: content.followup
                )
            }
        } else {
            message.text = "已停止生成"
            message.structuredContent = nil
        }
        message.state = .ready
        message.canRetry = false
        message.streamingStatusText = nil
        message.isGeneratingFollowups = false
        message.followupError = nil
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

    private func retryLast() {
        guard !isAssistantStreaming else { return }
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

    private func canStartSend(query: String, imageData: Data?) -> Bool {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return (!trimmed.isEmpty || imageData != nil) && !isAssistantStreaming
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

            // 结构化流式（新协议）：后端按"开场白逐字 → 每款商品解说 → 追问"顺序下发，
            // 卡片随自己的解说到达时才弹出，而不是检索一完成就整批砸出来。
            var hasStructuredStream = false
            var streamedOpening = ""
            var streamedItems: [StructuredItem] = []
            var streamedFollowups: [String] = []
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
                        lastStreamingStatusText = statusText
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
                            markFollowupGenerationInProgress("生成追问中")
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
                            hydrated: hydrated
                        )
                    } else {
                        pendingProductPreview = true
                        holdProductsForStructuredStream(query: query)
                    }

                case .openingDelta:
                    if let piece = event.textDelta {
                        hasStructuredStream = true
                        pendingProductPreview = false
                        setStreamingStatus(hydrated.isEmpty ? "正在整理推荐" : "正在介绍商品")
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
                        streamedItems.append(StructuredItem(productId: pid, description: description))
                        let remaining = hydrated.count - streamedItems.count
                        setStreamingStatus(remaining > 0 ? "正在介绍商品" : "生成追问中")
                        withAnimation(.interactiveSpring(response: 0.34, dampingFraction: 0.86)) {
                            renderStructuredStream(
                                query: query,
                                opening: streamedOpening,
                                items: streamedItems,
                                followups: streamedFollowups,
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
                        setStreamingStatus("生成追问中")
                        withAnimation(.easeOut(duration: 0.18)) {
                            renderStructuredStream(
                                query: query,
                                opening: streamedOpening,
                                items: streamedItems,
                                followups: streamedFollowups,
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
                    if hasStructuredStream {
                        finishStructuredStream(
                            query: query,
                            rawText: narrative,
                            opening: streamedOpening,
                            items: streamedItems,
                            followups: streamedFollowups,
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

    /// 商品数据已就绪、但解说还没开始：只摆一个开场占位，不提前把卡片全摊出来。
    private func holdProductsForStructuredStream(query: String) {
        guard latestAssistantMessage?.structuredContent == nil else { return }
        let opening = searchingRecommendationOpening(for: query)
        updateLastAI(
            text: opening,
            state: .generating,
            products: [],
            structuredContent: StructuredContent(opening: opening, items: []),
            isGeneratingFollowups: true
        )
    }

    /// 按"解说到达顺序"渲染：只有拿到专属解说的商品才出现卡片。
    private func renderStructuredStream(
        query: String,
        opening: String,
        items: [StructuredItem],
        followups: [String],
        hydrated: [Product]
    ) {
        let byID = Dictionary(hydrated.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        let visibleProducts = items.compactMap { byID[$0.productId] }
        let openingText = opening.isEmpty ? searchingRecommendationOpening(for: query) : opening
        updateLastAI(
            text: openingText,
            state: .generating,
            products: visibleProducts,
            structuredContent: StructuredContent(opening: openingText, items: items, followup: followups),
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
        let content = StructuredContent(opening: openingText, items: finalItems, followup: finalFollowups)
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
        // 已取消的回合可能刚从 sleep 醒来，不能覆盖 stopGeneration 的收尾或下一轮的消息
        guard !Task.isCancelled else { return }
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

    /// 把后端「请选择规格」的可交互卡片挂到当前 AI 消息上；后续 token/done
    /// 只更新文本与状态，不会清掉已挂上的卡片。
    private func attachSpecSelection(_ spec: SpecSelection) {
        guard !Task.isCancelled else { return }
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].specSelection = spec
    }

    /// 把对比结果挂到当前 AI 消息上，对话流里直接渲染对比卡片。
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
        // 优先使用结构化内容
        if let content = parsedStructuredContent {
            return content.opening
        }
        // 降级：从文本中取第一段
        return textParagraphs.first
    }
    
    private var followups: [String] {
        guard message.sender == .ai else { return [] }
        if let content = parsedStructuredContent {
            return content.followup.filter {
                !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
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
                    Label("已停止", systemImage: "stop.circle")
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
        .accessibilityLabel("生成中")
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 300_000_000)
                phase = (phase + 1) % 3
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
        let chineseEndings = [
            "和", "跟", "还有", "然后", "以及", "或者", "但是", "而且", "那个", "这个",
            "就是", "比如", "我想", "我要", "想要", "帮我", "呃", "嗯"
        ]
        if chineseEndings.contains(where: { trimmed.hasSuffix($0) }) {
            return true
        }
        let englishEndings: Set<String> = ["of", "and", "the", "to", "with", "for", "a", "an", "or", "but"]
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

    /// 权限都已授予，开麦不会弹系统权限框。
    static var isAuthorizedWithoutPrompt: Bool {
        SFSpeechRecognizer.authorizationStatus() == .authorized
            && AVAudioApplication.shared.recordPermission == .granted
    }

    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh_CN"))
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
                errorMessage = "没有检测到可用的麦克风。"
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
            errorMessage = "语音输入启动失败，请稍后重试。"
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
                errorMessage = "语音识别暂时不可用。"
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
                .accessibilityLabel("正在聆听")
                .accessibilityHint("点按结束说话并发送")
                .accessibilityAddTraits(.isButton)

            Button("取消", action: onCancel)
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
            Text("请说话")
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
            // 多规格显示完整区间，避免「¥480 起」让用户以为看中的规格也是这个价
            Text(product.priceRangeDisplay)
                .font(.title3.bold())
                .foregroundStyle(AppTheme.error)
            if let caption = product.budgetHint?.caption {
                Text(caption)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.textSecondary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(AppTheme.secondary.opacity(0.6), in: Capsule())
            }

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
