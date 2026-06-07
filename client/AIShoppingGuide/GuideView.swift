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
    @State private var selectedProduct: Product?
    @State private var comparisonContext: ComparisonContext?
    @State private var lastQuery: String = ""
    @State private var currentConversationID = UUID()
    @State private var currentSessionID = UUID().uuidString
    @State private var currentTitle = GuideView.newConversationTitle
    @State private var examples: [String] = GuideView.freshExamples()
    @State private var keyboardHeight: CGFloat = 0
    @State private var composerHeight: CGFloat = 56
    @State private var isAutoFollowEnabled = true
    @State private var isNearChatBottom = true
    @State private var isUserInteractingWithChat = false
    @State private var shouldShowJumpToLatest = false
    @State private var pendingQuestionAnchorID: UUID?
    @State private var shouldHoldLatestQuestionAnchor = false
    @State private var pendingAutoFollowWorkItem: DispatchWorkItem?
    @State private var photoPickerItem: PhotosPickerItem?
    @State private var showPhotoPicker = false
    @State private var showCamera = false
    /// 待发送的图片草稿：选/拍图后先挂在输入区，待用户配上文字一起发送；nil 表示无附件。
    @State private var pendingImageData: Data?
    @StateObject private var speechInput = SpeechInputController()
    @FocusState private var isInputFocused: Bool

    private let agentService = RESTAgentService()
    private let nearBottomThreshold: CGFloat = 96
    private let questionAnchorCharacterLimit = 220
    private let autoFollowTimer = Timer.publish(every: 0.12, on: .main, in: .common).autoconnect()

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

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottomLeading) {
                VStack(spacing: 0) {
                    header
                    chatList
                }

                composerStack
                    .padding(.horizontal, 16)
                    .padding(.bottom, composerBottomPadding)
                    .background(
                        GeometryReader { geometry in
                            Color.clear.preference(key: ComposerHeightPreferenceKey.self, value: geometry.size.height)
                        }
                    )
                    .zIndex(1)
            }
            .background(AppTheme.background)
            .onPreferenceChange(ComposerHeightPreferenceKey.self) { height in
                guard height > 0 else { return }
                composerHeight = height
            }
            .sheet(isPresented: $showHistory) {
                HistorySheet(
                    conversations: store.conversations,
                    onSelect: { openConversation($0) },
                    onDelete: { store.delete($0) }
                )
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
            }
            .navigationDestination(item: $selectedProduct) { product in
                ProductDetailView(product: product) { product, selectedOptions, quantity in
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
        }
    }

    private var header: some View {
        HStack {
            Text("CartPilot AI 导购助手")
                .font(.title3.bold())
                .foregroundStyle(AppTheme.textPrimary)
            Spacer()
            Button {
                startNewConversation()
            } label: {
                Image(systemName: "square.and.pencil")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(AppTheme.primary)
                    .frame(width: 40, height: 40)
                    .floatingLiquidPanel(cornerRadius: 20)
            }
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
            ZStack(alignment: .bottomTrailing) {
                trackedChatScrollView

                if shouldShowJumpToLatest {
                    jumpToLatestButton {
                        jumpToLatest(proxy)
                    }
                    .padding(.trailing, 18)
                    .padding(.bottom, jumpButtonBottomPadding)
                    .transition(.scale(scale: 0.92).combined(with: .opacity))
                }
            }
            .onAppear {
                scrollToLatestMessage(proxy, animated: false)
            }
            .onChange(of: pendingQuestionAnchorID) { _, newValue in
                guard let newValue else { return }
                scrollToQuestion(newValue, proxy: proxy)
            }
            .onChange(of: scrollAnchorToken) { _, _ in
                handleContentChange(proxy)
            }
            .onChange(of: isInputFocused) { _, focused in
                if focused, isAutoFollowEnabled || isNearChatBottom {
                    scheduleAutoFollowScroll(proxy, animated: true)
                }
            }
            .onReceive(autoFollowTimer) { _ in
                if isAssistantStreaming && isAutoFollowEnabled && !shouldKeepLatestQuestionVisible {
                    scrollToLatestMessage(proxy, animated: false)
                }
            }
        }
    }

    @ViewBuilder
    private var trackedChatScrollView: some View {
        if #available(iOS 18.0, *) {
            chatScrollView
                .onScrollGeometryChange(for: Bool.self) { geometry in
                    let distanceFromBottom = geometry.contentSize.height - geometry.visibleRect.maxY
                    return distanceFromBottom <= nearBottomThreshold
                } action: { _, isNearBottom in
                    updateScrollPosition(isNearBottom: isNearBottom)
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
                }
                ForEach(messages) { message in
                    MessageRow(
                        message: message,
                        examples: [],
                        onExampleTap: { send($0) },
                        onRetry: retryLast,
                        onProductTap: { selectedProduct = $0 },
                        onSpecSubmit: { send($0) },
                        onCompareTap: { comparisonContext = ComparisonContext(candidates: $0) }
                    )
                    .id(message.id)

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

    private func jumpToLatestButton(action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "arrow.down")
                    .font(.system(size: 13, weight: .bold))
                Text("最新")
                    .font(.caption.weight(.semibold))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(AppTheme.primary, in: Capsule())
            .shadow(color: AppTheme.primary.opacity(0.28), radius: 12, y: 5)
        }
        .buttonStyle(.plain)
    }

    /// 空态：欢迎语 + 随机示例气泡。仅在当前会话还没有任何消息时显示，
    /// 用户发起检索后随消息出现而隐去；开新对话会重新出现并换一批示例。
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("你可以问")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .stroke(AppTheme.border, lineWidth: 1)
                )

            VStack(alignment: .leading, spacing: 8) {
                ForEach(examples, id: \.self) { example in
                    Button(example) { send(example) }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(AppTheme.primary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 10)
                        .frame(maxWidth: 220, alignment: .leading)
                        .background(AppTheme.softPurple, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
                    .floatingLiquidPanel(cornerRadius: 22)
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
                        .rotationEffect(.degrees(isComposerExpanded ? 45 : 0))
                }
                .buttonStyle(.plain)

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
                .buttonStyle(.plain)

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
                .buttonStyle(.plain)
                .disabled(!canSend)
                .animation(.easeOut(duration: 0.18), value: canSend)
            }
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .floatingLiquidPanel(cornerRadius: 28)

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
        pendingImageData != nil ? "想找相似的？说说要求，比如「平价同款」…" : "想买点什么？和我聊聊…"
    }

    private var isKeyboardPresented: Bool {
        keyboardHeight > 0
    }

    private var composerBottomPadding: CGFloat {
        isKeyboardPresented ? 10 : AppTheme.guideComposerBottomPadding
    }

    private var chatListBottomPadding: CGFloat {
        composerHeight + composerBottomPadding + (isKeyboardPresented ? 16 : 24)
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
        isAutoFollowEnabled = true
        isNearChatBottom = false
        isUserInteractingWithChat = false
        shouldShowJumpToLatest = false
        shouldHoldLatestQuestionAnchor = true
        pendingQuestionAnchorID = userMessage.id
        messages.append(userMessage)
        let placeholder = imageData != nil ? "正在识别图片" : "正在理解你的需求"
        messages.append(ChatMessage(sender: .ai, text: placeholder, state: .understanding))
        runAgent(for: query, imageBase64: imageData?.base64EncodedString())
    }

    private func retryLast() {
        messages.removeAll { $0.canRetry }
        let query = lastQuery.isEmpty ? "重新推荐" : lastQuery
        pendingAutoFollowWorkItem?.cancel()
        isAutoFollowEnabled = true
        shouldHoldLatestQuestionAnchor = false
        shouldShowJumpToLatest = false
        messages.append(ChatMessage(sender: .ai, text: "正在重新理解你的需求", state: .understanding))
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
            var hydrated: [Product] = []
            var statusText = imageBase64 == nil ? "正在理解你的需求" : "正在识别图片"

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
                            updateLastAI(text: narrative, state: .generating)
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
                        updateLastAI(
                            text: narrative.isEmpty ? statusText : narrative,
                            state: .ready,
                            products: hydrated
                        )

                    default:
                        break
                    }
                }

                // 流正常结束但未收到 done 时的兜底
                if let index = messages.lastIndex(where: { $0.sender == .ai }),
                   messages[index].state != .ready, messages[index].state != .failed {
                    updateLastAI(
                        text: narrative.isEmpty ? statusText : narrative,
                        state: .ready,
                        products: hydrated
                    )
                }
                persistCurrent()
            } catch {
                updateLastAI(text: errorMessage(error), state: .failed, canRetry: true)
                persistCurrent()
            }
        }
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
    }

    private func syncCartItems(from snapshot: CartSnapshotPayload) {
        cartItems = snapshot.items.map { item in
            CartItem(
                product: Product(payload: item.product),
                selectedOptions: item.selectedOptions,
                quantity: item.quantity
            )
        }
    }

    private func errorMessage(_ error: Error) -> String {
        if let restError = error as? RESTServiceError {
            return restError.displayMessage
        }
        return "商品服务暂时不可用，请稍后重试。\n错误码：API_UNKNOWN_ERROR"
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
        if shouldKeepLatestQuestionVisible {
            return
        }

        shouldHoldLatestQuestionAnchor = false
        if isAutoFollowEnabled {
            scheduleAutoFollowScroll(proxy, animated: true)
        } else if !isNearChatBottom {
            shouldShowJumpToLatest = true
        }
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
        shouldHoldLatestQuestionAnchor = false
        isAutoFollowEnabled = true
        shouldShowJumpToLatest = false
        scheduleAutoFollowScroll(proxy, animated: true, delay: 0)
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

    private func scrollToLatestMessage(_ proxy: ScrollViewProxy, animated: Bool) {
        guard let latestMessageID = messages.last?.id else { return }
        let targetID = messageTailAnchorID(for: latestMessageID)
        let anchor = latestContentAnchor

        if animated {
            withAnimation(.easeOut(duration: 0.22)) {
                proxy.scrollTo(targetID, anchor: anchor)
            }
        } else {
            proxy.scrollTo(targetID, anchor: anchor)
        }
    }

    private var latestContentAnchor: UnitPoint {
        UnitPoint(x: 0.5, y: isKeyboardPresented ? 0.62 : 0.78)
    }

    private func messageTailAnchorID(for messageID: UUID) -> String {
        "guide-message-tail-\(messageID.uuidString)"
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
    let onSpecSubmit: (String) -> Void
    let onCompareTap: ([Product]) -> Void

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

                if !displayText.isEmpty {
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
                        }
                    }
                }

                if message.canRetry {
                    Button("重试", action: onRetry)
                        .buttonStyle(.borderedProminent)
                        .tint(AppTheme.primary)
                }

                if let spec = message.specSelection {
                    SpecSelectionCard(selection: spec, onSubmit: onSpecSubmit)
                }

                if let comparison = message.comparison {
                    ComparisonCard(comparison: comparison)
                }

                ForEach(message.products) { product in
                    ProductCard(product: product) {
                        onProductTap(product)
                    }
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
                    .buttonStyle(.plain)
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
                .frame(height: 300)

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
            .disabled(!allChosen || submitted)
        }
        .padding(14)
        .frame(maxWidth: 320, alignment: .leading)
        .floatingLiquidPanel(cornerRadius: 22)
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
    let conversations: [Conversation]
    let onSelect: (Conversation) -> Void
    let onDelete: (Conversation) -> Void

    @State private var pendingDelete: Conversation?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("历史记录")
                .font(.title3.bold())
                .padding(.horizontal, 20)
                .padding(.top, 16)

            if conversations.isEmpty {
                emptyState
            } else {
                List {
                    ForEach(conversations) { conversation in
                        Button {
                            onSelect(conversation)
                        } label: {
                            row(conversation)
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                        // 左滑出现删除按钮（全滑直接删除）
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button(role: .destructive) {
                                onDelete(conversation)
                            } label: {
                                Label("删除", systemImage: "trash")
                            }
                        }
                        // 长按弹出菜单删除（带确认）
                        .contextMenu {
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
