import AVFoundation
import Speech
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
    @State private var lastQuery: String = ""
    @State private var agentService = RESTAgentService()
    @StateObject private var speechPlayback = SpeechPlaybackController()
    @StateObject private var speechInput = SpeechInputController()
    @FocusState private var isInputFocused: Bool

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
                            onProductTap: { selectedProduct = $0 },
                            onSpeak: { speechPlayback.toggle(messageID: message.id, text: message.text) },
                            isSpeaking: speechPlayback.activeMessageID == message.id
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
            .onChange(of: messages.last?.text) { _, _ in
                if let last = messages.last?.id {
                    withAnimation(.easeOut(duration: 0.18)) {
                        proxy.scrollTo(last, anchor: .bottom)
                    }
                }
            }
        }
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
        speechPlayback.stop()
        lastQuery = query
        messages.append(ChatMessage(sender: .user, text: query))
        messages.append(ChatMessage(sender: .ai, text: "正在理解你的需求", state: .understanding))
        runAgent(for: query)
    }

    private func retryLast() {
        messages.removeAll { $0.canRetry }
        speechPlayback.stop()
        let query = lastQuery.isEmpty ? "重新推荐" : lastQuery
        messages.append(ChatMessage(sender: .ai, text: "正在重新理解你的需求", state: .understanding))
        runAgent(for: query)
    }

    private func runAgent(for query: String) {
        Task { @MainActor in
            var narrative = ""
            var hydrated: [Product] = []
            var comparison: ProductComparisonPayload?
            var statusText = "正在理解你的需求"
            var didFinish = false

            do {
                let request = AgentRequestPayload(text: query)
                for try await event in agentService.streamResponse(for: request) {
                    switch event.type {
                    case .status:
                        if let status = event.status {
                            statusText = status.message.isEmpty ? statusText : status.message
                            if narrative.isEmpty {
                                updateLastAI(
                                    text: statusText,
                                    state: status.phase.messageState,
                                    products: hydrated,
                                    comparison: comparison
                                )
                            }
                        }

                    case .products:
                        hydrated = event.products.map(Product.init(payload:))
                        updateLastAI(
                            text: narrative.isEmpty ? statusText : narrative,
                            state: didFinish ? .ready : .generating,
                            products: hydrated,
                            comparison: comparison
                        )

                    case .comparison:
                        comparison = event.comparison
                        updateLastAI(
                            text: narrative.isEmpty ? statusText : narrative,
                            state: didFinish ? .ready : .generating,
                            products: hydrated,
                            comparison: comparison
                        )

                    case .cartSnapshot:
                        if let snapshot = event.cartSnapshot {
                            syncCartItems(from: snapshot)
                        }

                    case .textDelta:
                        if let piece = event.textDelta {
                            narrative += piece
                            updateLastAI(
                                text: narrative,
                                state: .generating,
                                products: hydrated,
                                comparison: comparison
                            )
                        }

                    case .done:
                        didFinish = true
                        updateLastAI(
                            text: narrative.isEmpty ? statusText : narrative,
                            state: .ready,
                            products: hydrated,
                            comparison: comparison
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
                        products: hydrated,
                        comparison: comparison
                    )
                }
            } catch {
                updateLastAI(text: errorMessage(error), state: .failed, canRetry: true)
            }
        }
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
        comparison: ProductComparisonPayload? = nil,
        canRetry: Bool = false
    ) {
        guard let index = messages.lastIndex(where: { $0.sender == .ai }) else { return }
        messages[index].text = text
        messages[index].state = state
        messages[index].products = products
        messages[index].comparison = comparison
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
    let onSpeak: () -> Void
    let isSpeaking: Bool

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

                if canSpeak {
                    Button(action: onSpeak) {
                        Label(isSpeaking ? "停止朗读" : "朗读回复", systemImage: isSpeaking ? "speaker.slash.fill" : "speaker.wave.2.fill")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(AppTheme.primary)
                            .padding(.horizontal, 11)
                            .padding(.vertical, 7)
                            .background(AppTheme.softPurple, in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(isSpeaking ? "停止朗读 AI 回复" : "朗读 AI 回复")
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

                if let comparison = message.comparison {
                    ComparisonTable(comparison: comparison, products: message.products)
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

    private var canSpeak: Bool {
        message.sender == .ai
            && message.state == .ready
            && !message.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && examples.isEmpty
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
final class SpeechPlaybackController: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published private(set) var activeMessageID: UUID?

    private let synthesizer = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func toggle(messageID: UUID, text: String) {
        if activeMessageID == messageID {
            stop()
            return
        }
        stop()

        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else { return }

        let utterance = AVSpeechUtterance(string: cleanText)
        utterance.voice = AVSpeechSynthesisVoice(language: "zh-CN")
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        activeMessageID = messageID
        synthesizer.speak(utterance)
    }

    func stop() {
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        activeMessageID = nil
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer,
        didFinish utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in
            activeMessageID = nil
        }
    }

    nonisolated func speechSynthesizer(
        _ synthesizer: AVSpeechSynthesizer,
        didCancel utterance: AVSpeechUtterance
    ) {
        Task { @MainActor in
            activeMessageID = nil
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

struct ComparisonTable: View {
    let comparison: ProductComparisonPayload
    let products: [Product]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(comparison.title)
                .font(.headline)
                .foregroundStyle(AppTheme.textPrimary)

            VStack(spacing: 0) {
                ForEach(comparison.rows) { row in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(row.label)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(AppTheme.textSecondary)

                        ForEach(Array(row.values.enumerated()), id: \.offset) { index, value in
                            HStack(alignment: .top, spacing: 8) {
                                Text(productName(for: index, productID: value.productID))
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(AppTheme.primary)
                                    .frame(width: 72, alignment: .leading)
                                    .lineLimit(1)
                                    .truncationMode(.tail)

                                Text(value.text)
                                    .font(.caption)
                                    .foregroundStyle(AppTheme.textPrimary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .padding(.vertical, 10)

                    if row.id != comparison.rows.last?.id {
                        Divider().overlay(AppTheme.border)
                    }
                }
            }
        }
        .padding(14)
        .floatingLiquidPanel(cornerRadius: 18)
    }

    private func productName(for index: Int, productID: String) -> String {
        if let brand = brandName(for: productID), !brand.isEmpty, brand != "—" {
            return brand
        }
        if index < products.count {
            return compactName(for: products[index], fallbackIndex: index)
        }
        if let product = products.first(where: { $0.id == productID }) {
            return compactName(for: product, fallbackIndex: index)
        }
        return "商品\(index + 1)"
    }

    private func compactName(for product: Product, fallbackIndex: Int) -> String {
        let title = product.title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else {
            return "商品\(fallbackIndex + 1)"
        }
        if title.count <= 8 {
            return title
        }
        return String(title.prefix(8)) + "…"
    }

    private func brandName(for productID: String) -> String? {
        guard let row = comparison.rows.first(where: { $0.label == "品牌" }) else {
            return nil
        }
        return row.values.first(where: { $0.productID == productID })?.text
            .trimmingCharacters(in: .whitespacesAndNewlines)
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
