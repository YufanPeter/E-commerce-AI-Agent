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
    @FocusState private var inputFocused: Bool

    private let examples = ["I have oily skin", "I usually wear size 9", "I avoid Nike", "My budget is usually between $30 and $50", "I am allergic to latex"]

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 24) {
                    if preferenceStore.loadFailed {
                        failureCard
                    } else if let memory = preferenceStore.memory {
                        overview(memory)
                        tellSection(memory)
                        if !memory.suggestions.isEmpty { suggestionsSection(memory.suggestions) }
                        if memory.groups.isEmpty && memory.suggestions.isEmpty {
                            emptyExplanation
                        } else {
                            ForEach(memory.groups) { groupSection($0) }
                        }
                        if !memory.activity.isEmpty { activitySection(memory.activity) }
                        settingsSection(memory)
                    } else {
                        ProgressView()
                            .tint(AppTheme.primary)
                            .frame(maxWidth: .infinity, minHeight: 240)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 2)
                .padding(.bottom, 120)
            }
            .scrollDismissesKeyboard(.interactively)
            .background(AppTheme.background)
            .navigationTitle("Memory")
            .overlay(alignment: .bottom) { toastView }
            .task { await preferenceStore.load() }
            .onChange(of: isVisible) { _, visible in
                if visible { Task { await preferenceStore.load() } }
            }
            .confirmationDialog(
                "Clear all memories?",
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
            Text(memory.count == 0 ? "No memories yet" : "\(memory.count) saved memories")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
            if !memory.personalizationEnabled {
                Label("Recommendations are not using memory", systemImage: "pause.circle")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.primary)
            }
        }
        // 和系统大标题的左边缘对齐（标题比内容区靠左一点）
        .padding(.leading, -3)
    }

    // MARK: 直接说

    private func tellSection(_ memory: MemoryViewPayload) -> some View {
        let canSend = !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !preferenceStore.isSending
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                TextField("Tell your shopping assistant something", text: $draft, axis: .vertical)
                    .lineLimit(1...3)
                    .font(.subheadline)
                    .focused($inputFocused)
                    .submitLabel(.send)
                    .onSubmit(send)
                Button(action: send) {
                    Group {
                        if preferenceStore.isSending {
                            ProgressView().tint(.white)
                        } else {
                            Image(systemName: "arrow.up")
                                .font(.system(size: 14, weight: .bold))
                        }
                    }
                    .foregroundStyle(.white)
                    .frame(width: 32, height: 32)
                    .background(canSend || preferenceStore.isSending ? AppTheme.primary : AppTheme.textSecondary.opacity(0.35), in: Circle())
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.tactile)
                .disabled(!canSend)
                .accessibilityLabel("Save a memory")
            }
            .padding(.leading, 16)
            .padding(.trailing, 6)
            .frame(minHeight: 52)
            .surfacePanel(cornerRadius: 26)

            if memory.count == 0 {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(examples, id: \.self) { example in
                            Button(example) { draft = example; inputFocused = true }
                                .font(.footnote)
                                .foregroundStyle(AppTheme.primary)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 8)
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
            sectionTitle("Suggestions to remember")
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    if index > 0 { rowDivider }
                    suggestionRow(item)
                }
            }
            .surfacePanel(cornerRadius: 18)
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
                        .foregroundStyle(.white)
                        .padding(.horizontal, 20)
                        .frame(minHeight: 36)
                        .background(AppTheme.primary, in: Capsule())
                    Button("Dismiss") { Task { await preferenceStore.reject(item) } }
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
            .surfacePanel(cornerRadius: 18)
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
                    .foregroundStyle(item.needsCare ? AppTheme.error : AppTheme.primary)
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
            .foregroundStyle(item.needsCare ? AppTheme.error : AppTheme.primary)
            .frame(width: 32, height: 32)
            .background(AppTheme.secondary, in: Circle())
            .padding(.top, topInset)
    }

    private func memorySymbol(_ kind: String) -> String {
        switch kind {
        case "budget", "price_tier": return "yensign"
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
            return "\(item.sourceLabel)：\(evidence)"
        }
        return item.sourceLabel
    }

    private func rowEffect(_ item: MemoryItemPayload) -> String {
        item.useCount > 0 ? "\(item.effect) · Used in \(item.useCount) recommendations" : item.effect
    }

    // MARK: 空状态

    private var emptyExplanation: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "text.bubble")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(AppTheme.primary)
                .frame(width: 32, height: 32)
                .background(AppTheme.secondary, in: Circle())
            VStack(alignment: .leading, spacing: 4) {
                Text("Tell us what matters to you")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                Text("Save preferences such as your size or brands to avoid. One-time requests stay in the conversation.")
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .surfacePanel(cornerRadius: 18)
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
                        .foregroundStyle(AppTheme.primary)
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
        .surfacePanel(cornerRadius: 18)
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

    // MARK: 设置

    private func settingsSection(_ memory: MemoryViewPayload) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Toggle(isOn: Binding(
                get: { memory.personalizationEnabled },
                set: { enabled in Task { await preferenceStore.setPersonalization(enabled) } }
            )) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Use memory in recommendations")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(AppTheme.textPrimary)
                    Text(memory.personalizationEnabled ? "Turning this off keeps your saved memories." : "Recommendations are not using your memories.")
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                }
            }
            .tint(AppTheme.primary)
            .padding(14)

            if memory.count > 0 || !memory.activity.isEmpty {
                Divider().padding(.leading, 14)
                Button { showClearConfirm = true } label: {
                    Text("Clear all memories")
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
        .surfacePanel(cornerRadius: 18)
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
            Text("Could not connect to load memory.")
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
            .font(.footnote.weight(.semibold))
            .foregroundStyle(AppTheme.textSecondary)
            .padding(.horizontal, 2)
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
