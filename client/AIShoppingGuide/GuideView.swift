import AVFoundation
import Speech
import SwiftUI

struct GuideView: View {
    @Binding var cartItems: [CartItem]
    @StateObject private var store = ConversationStore()
    @State private var messages: [ChatMessage] = []
    @State private var inputText = ""
    @State private var isComposerExpanded = false
    @State private var showHistory = false
    @State private var selectedProduct: Product?
    @State private var lastQuery: String = ""
    @State private var currentConversationID = UUID()
    @State private var currentSessionID = UUID().uuidString
    @State private var currentTitle = GuideView.newConversationTitle
    @State private var examples: [String] = GuideView.freshExamples()
    @StateObject private var speechInput = SpeechInputController()
    @FocusState private var isInputFocused: Bool

    private let agentService = RESTAgentService()

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
                    .padding(.bottom, AppTheme.guideComposerBottomPadding)
                    .zIndex(1)
            }
            .background(AppTheme.background)
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
            ScrollView(showsIndicators: false) {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if messages.isEmpty {
                        emptyState
                    }
                    ForEach(messages) { message in
                        MessageRow(
                            message: message,
                            examples: [],
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
                            ComposerAction(icon: "camera.fill", title: "相机") {}
                            ComposerAction(icon: "photo.fill", title: "图片上传") {}
                        }
                    }
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                } else {
                    VStack(alignment: .leading, spacing: 12) {
                        ComposerAction(icon: "camera.fill", title: "相机") {}
                        ComposerAction(icon: "photo.fill", title: "图片上传") {}
                    }
                    .padding(14)
                    .frame(width: 156, alignment: .leading)
                    .floatingLiquidPanel(cornerRadius: 22)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                }
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

                TextField("想买点什么？和我聊聊…", text: $inputText)
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
        !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func sendCurrentInput() {
        let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        speechInput.stop()
        inputText = ""
        isInputFocused = false
        send(trimmed)
    }

    private func send(_ query: String) {
        isComposerExpanded = false
        speechInput.stop()
        lastQuery = query
        if currentTitle == GuideView.newConversationTitle {
            currentTitle = String(query.prefix(20))
        }
        messages.append(ChatMessage(sender: .user, text: query))
        messages.append(ChatMessage(sender: .ai, text: "正在理解你的需求", state: .understanding))
        runAgent(for: query)
    }

    private func retryLast() {
        messages.removeAll { $0.canRetry }
        let query = lastQuery.isEmpty ? "重新推荐" : lastQuery
        messages.append(ChatMessage(sender: .ai, text: "正在重新理解你的需求", state: .understanding))
        runAgent(for: query)
    }

    private func runAgent(for query: String) {
        Task { @MainActor in
            var narrative = ""
            var hydrated: [Product] = []
            var statusText = "正在理解你的需求"

            do {
                let request = AgentRequestPayload(sessionID: currentSessionID, text: query)
                for try await event in agentService.streamResponse(for: request) {
                    switch event.type {
                    case .status:
                        if let status = event.status {
                            statusText = status.message.isEmpty ? statusText : status.message
                            if narrative.isEmpty {
                                updateLastAI(
                                    text: statusText,
                                    state: status.phase.messageState,
                                    products: hydrated
                                )
                            }
                        }

                    case .products:
                        hydrated = event.products.map(Product.init(payload:))
                        updateLastAI(
                            text: narrative.isEmpty ? statusText : narrative,
                            state: .generating,
                            products: hydrated
                        )

                    case .cartSnapshot:
                        if let snapshot = event.cartSnapshot {
                            syncCartItems(from: snapshot)
                        }

                    case .textDelta:
                        if let piece = event.textDelta {
                            narrative += piece
                            updateLastAI(text: narrative, state: .generating, products: hydrated)
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
