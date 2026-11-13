import SwiftUI

/// 记忆页的状态。`preference` 是检索实际使用的偏好，`memory` 是页面展示的记忆台账。
@MainActor
final class PreferenceStore: ObservableObject {
    struct Toast: Identifiable, Equatable {
        let id = UUID()
        let message: String
        let undoToken: String?
    }

    @Published private(set) var memory: MemoryViewPayload?
    @Published private(set) var preference: UserPreferencePayload = .empty()
    @Published private(set) var isLoading = false
    @Published private(set) var isSending = false
    @Published private(set) var loadFailed = false
    @Published var toast: Toast?

    private let service: any PreferenceServicing
    private let userID: String

    init(
        userID: String = UserIdentity.defaultUserID,
        service: any PreferenceServicing = RESTPreferenceService()
    ) {
        self.userID = userID
        self.service = service
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            memory = try await service.fetchMemory(userID: userID)
            loadFailed = false
        } catch {
            loadFailed = memory == nil
        }
    }

    /// 导购对话里刚记住了什么：更新页面，保持和横幅一致。
    func applyMemoryUpdate(_ update: MemoryUpdatePayload) {
        if let remote = update.preference {
            preference = remote
        }
        Task { await load() }
    }

    func undo(_ update: MemoryUpdatePayload) async {
        await undo(token: update.undoToken)
    }

    func undo(token: String) async {
        await perform(failure: "Could not undo that change. Try again.") {
            try await self.service.undoMemory(userID: self.userID, undoToken: token)
        }
    }

    /// 返回是否成功，失败时保留输入框内容。
    func tell(_ text: String) async -> Bool {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return false }
        isSending = true
        defer { isSending = false }
        return await perform(failure: "Could not save the memory. Check the connection and try again.") {
            try await self.service.tellMemory(userID: self.userID, text: clean)
        }
    }

    func confirm(_ item: MemoryItemPayload) async {
        await perform(failure: "That action failed. Try again.") {
            try await self.service.confirmMemory(userID: self.userID, itemID: item.id)
        }
    }

    func reject(_ item: MemoryItemPayload) async {
        await perform(failure: "That action failed. Try again.") {
            try await self.service.rejectMemory(userID: self.userID, itemID: item.id)
        }
    }

    func forget(_ item: MemoryItemPayload) async {
        await perform(failure: "Could not delete the memory. Try again.") {
            try await self.service.deleteMemory(userID: self.userID, itemID: item.id)
        }
    }

    func setPersonalization(_ enabled: Bool) async {
        do {
            memory = try await service.setPersonalization(userID: userID, enabled: enabled)
        } catch {
            toast = Toast(message: "Could not save settings. Try again.", undoToken: nil)
        }
    }

    /// Forget what one earlier conversation was about. Quiet when the chat list deletes it.
    func forgetConversation(sessionID: String, announce: Bool = true) async {
        do {
            memory = try await service.forgetConversation(userID: userID, sessionID: sessionID)
            if announce {
                toast = Toast(message: "Forgot that conversation", undoToken: nil)
            }
        } catch {
            if announce {
                toast = Toast(message: "Could not forget the conversation. Try again.", undoToken: nil)
            }
        }
    }

    func clearAll() async {
        do {
            memory = try await service.clearMemory(userID: userID)
            toast = Toast(message: "All memories cleared", undoToken: nil)
        } catch {
            toast = Toast(message: "Could not clear memories. Try again.", undoToken: nil)
        }
    }

    @discardableResult
    private func perform(failure: String, _ action: @escaping () async throws -> MemoryActionResult) async -> Bool {
        do {
            let result = try await action()
            memory = result.view
            toast = Toast(message: result.message, undoToken: result.undoToken)
            return true
        } catch {
            toast = Toast(message: failure, undoToken: nil)
            return false
        }
    }
}

struct PreferenceView: View {
    @ObservedObject var preferenceStore: PreferenceStore
    var isVisible: Bool = true

    @State private var draft = ""
    @State private var showClearConfirm = false
    @State private var showActivity = false
    @State private var orders: [OrderPayload] = []
    @State private var orderImages: [String: URL] = [:]
    @State private var ordersLoaded = false
    @FocusState private var inputFocused: Bool

    private let orderService = RESTProductService()

    private let examples = ["I have oily skin", "I usually wear size 9", "I avoid Nike", "My budget is usually between $30 and $50", "I am allergic to latex"]

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 28) {
                    ordersSection
                    if preferenceStore.loadFailed {
                        failureCard
                    } else if let memory = preferenceStore.memory {
                        VStack(alignment: .leading, spacing: 4) {
                            sectionTitle("My preferences")
                            overview(memory)
                        }
                        tellSection(memory)
                        if !memory.suggestions.isEmpty { suggestionsSection(memory.suggestions) }
                        ForEach(memory.groups) { groupSection($0) }
                        if !memory.activity.isEmpty { activitySection(memory.activity) }
                        if let talks = memory.conversations, !talks.isEmpty { conversationsSection(talks) }
                        settingsSection(memory)
                    } else {
                        ProgressView()
                            .tint(AppTheme.primary)
                            .frame(maxWidth: .infinity, minHeight: 240)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 120)
            }
            .scrollDismissesKeyboard(.interactively)
            .background(AppTheme.background)
            .navigationTitle("Me")
            .overlay(alignment: .bottom) { toastView }
            .task {
                await preferenceStore.load()
                await loadOrders()
            }
            .onChange(of: isVisible) { _, visible in
                if visible {
                    Task {
                        await preferenceStore.load()
                        await loadOrders()
                    }
                }
            }
            .confirmationDialog(
                "Clear all preferences?",
                isPresented: $showClearConfirm,
                titleVisibility: .visible
            ) {
                Button("Clear permanently", role: .destructive) {
                    Task { await preferenceStore.clearAll() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("This removes your preferences and recent activity.")
            }
        }
    }

    // MARK: 概览

    /// 标题下的一句话。具体记了什么下面都列着，这里不再重复。
    private func overview(_ memory: MemoryViewPayload) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(memory.count == 0 ? "Nothing saved yet. Tell CartPilot what matters to you." : "\(memory.count) saved · used to tailor recommendations")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
            if !memory.personalizationEnabled {
                Label("Recommendations are not using your preferences", systemImage: "pause.circle")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.primary)
            }
        }
    }

    // MARK: 直接说

    private func tellSection(_ memory: MemoryViewPayload) -> some View {
        let canSend = !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !preferenceStore.isSending
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                TextField("e.g. I wear size 9, I avoid latex", text: $draft, axis: .vertical)
                    .lineLimit(1...3)
                    .font(.subheadline)
                    .focused($inputFocused)
                    .submitLabel(.send)
                    .onSubmit(send)
                Button(action: send) {
                    Group {
                        if preferenceStore.isSending {
                            ProgressView().tint(AppTheme.background)
                        } else {
                            Image(systemName: "arrow.up")
                                .font(.system(size: 14, weight: .bold))
                        }
                    }
                    .foregroundStyle(AppTheme.background)
                    .frame(width: 32, height: 32)
                    .background(canSend || preferenceStore.isSending ? AppTheme.primary : AppTheme.textTertiary, in: Circle())
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.tactile)
                .disabled(!canSend)
                .accessibilityLabel("Save preference")
            }
            .padding(.leading, 16)
            .padding(.trailing, 6)
            .frame(minHeight: 52)
            .surfacePanel(cornerRadius: 12)

            if memory.count == 0 {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(examples, id: \.self) { example in
                            Button(example) { draft = example; inputFocused = true }
                                .font(.footnote)
                                .foregroundStyle(AppTheme.textPrimary)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 7)
                                .background(AppTheme.secondary, in: Capsule())
                                .buttonStyle(.tactile)
                        }
                    }
                    .padding(.horizontal, 2)
                }
            }
        }
    }

    private func send() {
        let text = draft
        Task {
            if await preferenceStore.tell(text) {
                draft = ""
                inputFocused = false
            }
        }
    }

    // MARK: 待确认

    private func suggestionsSection(_ items: [MemoryItemPayload]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                AIByline(title: "Suggested from your activity")
            }
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { rowDivider }
                    suggestionRow(item)
                }
            }
            .surfacePanel(cornerRadius: 12)
        }
    }

    private func suggestionRow(_ item: MemoryItemPayload) -> some View {
        HStack(alignment: .top, spacing: 12) {
            memoryIcon(item, topInset: 0)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.label)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                if let evidence = item.evidence, !evidence.isEmpty {
                    Text(item.source == "inferred" ? evidence : "You said: \(evidence)")
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("If confirmed: \(item.effect)")
                    .font(.caption)
                    .foregroundStyle(item.needsCare ? AppTheme.error : AppTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("Save") { Task { await preferenceStore.confirm(item) } }
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(AppTheme.background)
                        .padding(.horizontal, 20)
                        .frame(minHeight: 36)
                        .background(AppTheme.primary, in: Capsule())
                    Button("Not now") { Task { await preferenceStore.reject(item) } }
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(AppTheme.textSecondary)
                        .padding(.horizontal, 14)
                        .frame(minHeight: 36)
                }
                .buttonStyle(.tactile)
                .padding(.top, 6)
            }
            Spacer(minLength: 0)
        }
        .padding(14)
    }

    // MARK: 生效的记忆

    private func groupSection(_ group: MemoryGroupPayload) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle(group.name)
            VStack(spacing: 0) {
                ForEach(Array(group.items.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { rowDivider }
                    memoryRow(item)
                }
            }
            .surfacePanel(cornerRadius: 12)
        }
    }

    private func memoryRow(_ item: MemoryItemPayload) -> some View {
        HStack(alignment: .top, spacing: 12) {
            memoryIcon(item)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.label)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(rowProvenance(item))
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(rowEffect(item))
                    .font(.caption)
                    .foregroundStyle(item.needsCare ? AppTheme.error : AppTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 14)
            Spacer(minLength: 0)
            Button {
                Task { await preferenceStore.forget(item) }
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(AppTheme.textSecondary.opacity(0.75))
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.tactile)
            .accessibilityLabel("Forget \(item.label)")
            .padding(.top, 2)
        }
        .padding(.leading, 14)
        .padding(.trailing, 2)
    }

    private func memoryIcon(_ item: MemoryItemPayload, topInset: CGFloat = 12) -> some View {
        Image(systemName: memorySymbol(item.kind))
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(item.needsCare ? AppTheme.error : AppTheme.textPrimary)
            .frame(width: 32, height: 32)
            .background(AppTheme.secondary, in: Circle())
            .padding(.top, topInset)
    }

    private func memorySymbol(_ kind: String) -> String {
        switch kind {
        case "budget", "price_tier": return "dollarsign"
        case "brand_like": return "heart"
        case "brand_avoid": return "nosign"
        case "avoid_term": return "exclamationmark.shield"
        case "category": return "square.grid.2x2"
        default: return "person"
        }
    }

    private func rowProvenance(_ item: MemoryItemPayload) -> String {
        if let evidence = item.evidence, !evidence.isEmpty, item.source != "inferred" {
            return "You said: \(evidence)"
        }
        if let evidence = item.evidence, !evidence.isEmpty {
            return "\(item.sourceLabel): \(evidence)"
        }
        return item.sourceLabel
    }

    private func rowEffect(_ item: MemoryItemPayload) -> String {
        item.useCount > 0 ? "\(item.effect) · Used in \(item.useCount) recommendations" : item.effect
    }

    // MARK: 最近的行为

    private func activitySection(_ activity: [MemoryActivityPayload]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeOut(duration: 0.18)) { showActivity.toggle() }
            } label: {
                HStack(spacing: 12) {
                    Image(systemName: "clock")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(AppTheme.textPrimary)
                        .frame(width: 32, height: 32)
                        .background(AppTheme.secondary, in: Circle())
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Recent activity")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                        Text("Activity can suggest preferences for you to review.")
                            .font(.caption)
                            .foregroundStyle(AppTheme.textSecondary)
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(AppTheme.textSecondary)
                        .rotationEffect(.degrees(showActivity ? 180 : 0))
                }
                .padding(14)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showActivity {
                rowDivider
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(activity) { event in
                        activityRow(event)
                    }
                }
                .padding(.leading, 58)
                .padding(.trailing, 14)
                .padding(.vertical, 14)
                .transition(.opacity)
            }
        }
        .surfacePanel(cornerRadius: 12)
    }

    private func activityRow(_ event: MemoryActivityPayload) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(event.text)
                .font(.footnote)
                .foregroundStyle(AppTheme.textPrimary)
                .lineLimit(1)
            Spacer(minLength: 8)
            if let when = MemoryActivityTime.relative(from: event.at) {
                Text(when)
                    .font(.caption2)
                    .foregroundStyle(AppTheme.textSecondary)
                    .fixedSize()
            }
        }
    }

    // MARK: 记得的对话

    private func conversationsSection(_ talks: [MemoryConversationPayload]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "bubble.left.and.text.bubble.right")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(AppTheme.textPrimary)
                    .frame(width: 32, height: 32)
                    .background(AppTheme.secondary, in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text("Conversations I remember")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)
                    Text("Ask \"what did we talk about?\" or pick one up again.")
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                }
            }
            .padding(14)
            ForEach(talks) { talk in
                rowDivider
                conversationRow(talk)
            }
        }
        .surfacePanel(cornerRadius: 12)
    }

    private func conversationRow(_ talk: MemoryConversationPayload) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(talk.title)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(1)
                Text(talk.summary)
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(3)
                if let when = MemoryActivityTime.relative(from: talk.at) {
                    Text(when)
                        .font(.caption2)
                        .foregroundStyle(AppTheme.textSecondary)
                }
            }
            Spacer(minLength: 8)
            Button {
                Task { await preferenceStore.forgetConversation(sessionID: talk.sessionID) }
            } label: {
                Image(systemName: "xmark")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(AppTheme.textSecondary)
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Forget this conversation")
        }
        .padding(.leading, 58)
        .padding(.trailing, 10)
        .padding(.vertical, 12)
    }

    // MARK: 设置

    private func settingsSection(_ memory: MemoryViewPayload) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Toggle(isOn: Binding(
                get: { memory.personalizationEnabled },
                set: { enabled in Task { await preferenceStore.setPersonalization(enabled) } }
            )) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Personalize recommendations")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(AppTheme.textPrimary)
                    Text(memory.personalizationEnabled ? "Turning this off keeps what you saved." : "Recommendations ignore your saved preferences.")
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                }
            }
            .tint(AppTheme.primary)
            .padding(14)

            if memory.count > 0 || !memory.activity.isEmpty {
                Divider().padding(.leading, 14)
                Button { showClearConfirm = true } label: {
                    Text("Clear all preferences")
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.error)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 14)
                        .frame(minHeight: 48)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .surfacePanel(cornerRadius: 12)
    }

    // MARK: 通用

    /// 分隔线从图标右侧开始，文字列对齐，图标列保持干净。
    private var rowDivider: some View {
        Divider().padding(.leading, 58)
    }

    private var failureCard: some View {
        VStack(spacing: 12) {
            Image(systemName: "wifi.exclamationmark")
                .font(.title2)
                .foregroundStyle(AppTheme.textSecondary)
            Text("Could not load your preferences.")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
            Button("Try again") { Task { await preferenceStore.load() } }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.primary)
                .frame(minWidth: 88, minHeight: 44)
                .buttonStyle(.plain)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }

    private func sectionTitle(_ text: String) -> some View {
        Text(text)
            .font(.headline)
            .foregroundStyle(AppTheme.textPrimary)
    }

    // MARK: 订单

    private func loadOrders() async {
        guard let result = try? await orderService.fetchOrders() else {
            ordersLoaded = true
            return
        }
        orders = result.orders
        orderImages = result.images
        ordersLoaded = true
    }

    private var ordersSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle("Orders")
            if orders.isEmpty {
                HStack(spacing: 12) {
                    Image(systemName: "shippingbox")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(AppTheme.textPrimary)
                        .frame(width: 32, height: 32)
                        .background(AppTheme.secondary, in: Circle())
                    Text(ordersLoaded ? "No orders yet. Orders you place from the cart show up here." : "Loading orders…")
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .padding(14)
                .surfacePanel(cornerRadius: 12)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(orders.prefix(3).enumerated()), id: \.element.id) { index, order in
                        if index > 0 { Divider().padding(.leading, 14) }
                        orderRow(order)
                    }
                }
                .surfacePanel(cornerRadius: 12)
            }
        }
    }

    private func orderRow(_ order: OrderPayload) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(order.status == "placed" ? "Order placed" : order.status.capitalized)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                Spacer(minLength: 8)
                if let when = MemoryActivityTime.relative(from: String(order.createdAt.prefix(19))) {
                    Text(when)
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                }
            }
            HStack(spacing: 8) {
                ForEach(Array(order.lines.prefix(4).enumerated()), id: \.offset) { _, line in
                    ProductRemoteImage(url: orderImages[line.productID], cornerRadius: 8, placeholderIcon: "shippingbox", contentMode: .fit)
                        .padding(4)
                        .frame(width: 56, height: 56)
                        .background(Color.white, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .stroke(AppTheme.border, lineWidth: 0.5)
                        )
                }
                if order.lines.count == 1, let line = order.lines.first {
                    Text(line.title)
                        .font(.footnote)
                        .foregroundStyle(AppTheme.textSecondary)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
            }
            Text("\(order.itemCount) \(order.itemCount == 1 ? "item" : "items") · $\(String(format: "%.2f", order.total))")
                .font(.footnote)
                .foregroundStyle(AppTheme.textPrimary)
        }
        .padding(14)
    }

    // MARK: 操作反馈

    @ViewBuilder
    private var toastView: some View {
        if let toast = preferenceStore.toast {
            HStack(spacing: 12) {
                Text(toast.message)
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(AppTheme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                if let token = toast.undoToken {
                    Button("Undo") {
                        preferenceStore.toast = nil
                        Task { await preferenceStore.undo(token: token) }
                    }
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(AppTheme.primary)
                    .frame(minWidth: 44, minHeight: 44)
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .frame(minHeight: 48)
            .floatingLiquidPanel(cornerRadius: 14)
            .padding(.horizontal, 20)
            .padding(.bottom, AppTheme.cartCheckoutBottomPadding)
            .id(toast.id)
            .task(id: toast.id) {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if preferenceStore.toast?.id == toast.id {
                    withAnimation(.easeOut(duration: 0.18)) { preferenceStore.toast = nil }
                }
            }
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }
}

/// 把后端的事件时间（UTC，`yyyy-MM-dd HH:mm:ss`）换成"3 小时前"。解析不了就不显示。
enum MemoryActivityTime {
    private static let parser: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    private static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.unitsStyle = .short
        return formatter
    }()

    static func relative(from raw: String, now: Date = Date()) -> String? {
        guard let date = parser.date(from: raw) else { return nil }
        if now.timeIntervalSince(date) < 60 { return "Just now" }
        return relative.localizedString(for: date, relativeTo: now)
    }
}

extension MemoryViewPayload {
    static func empty(userID: String = UserIdentity.defaultUserID) -> MemoryViewPayload {
        MemoryViewPayload(userID: userID, personalizationEnabled: true, summary: "", groups: [], suggestions: [], activity: [], count: 0)
    }

    static let preview = MemoryViewPayload(
        userID: "preview",
        personalizationEnabled: true,
        summary: "Oily skin, avoid Nike",
        groups: [
            MemoryGroupPayload(name: "About you", items: [
                MemoryItemPayload(id: "1", kind: "attribute", group: "About you", label: "Skin type: oily", scope: "Beauty & Personal Care", source: "stated", sourceLabel: "Stated in chat", evidence: "I have oily skin. Which face wash should I use?", effect: "Used to explain recommendations", needsCare: false, useCount: 0),
            ]),
            MemoryGroupPayload(name: "Brands", items: [
                MemoryItemPayload(id: "2", kind: "brand_avoid", group: "Brands", label: "Avoid Nike", scope: nil, source: "stated", sourceLabel: "Stated in chat", evidence: "I avoid Nike", effect: "Excluded from recommendations", needsCare: false, useCount: 3),
            ]),
        ],
        suggestions: [
            MemoryItemPayload(id: "3", kind: "avoid_term", group: "About you", label: "Avoid latex", scope: nil, source: "stated", sourceLabel: "Stated in chat", evidence: "I am allergic to latex", effect: "Products containing this ingredient are excluded", needsCare: true, useCount: 0),
            MemoryItemPayload(id: "4", kind: "brand_like", group: "Brands", label: "Likes Garmin", scope: nil, source: "inferred", sourceLabel: "Inferred from activity", evidence: "2 cart adds and 3 detail views", effect: "Preferred in ranking", needsCare: false, useCount: 0),
        ],
        activity: [
            MemoryActivityPayload(id: "1", type: "add_cart", text: "Added a Garmin watch", at: ""),
        ],
        count: 2
    )
}

#Preview("Memory") {
    PreferenceView(preferenceStore: PreferenceStore(service: MockPreferenceService()))
}
