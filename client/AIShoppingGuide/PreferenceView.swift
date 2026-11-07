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
        await perform(failure: "撤销失败，请再试一次") {
            try await self.service.undoMemory(userID: self.userID, undoToken: token)
        }
    }

    /// 返回是否成功，失败时保留输入框内容。
    func tell(_ text: String) async -> Bool {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return false }
        isSending = true
        defer { isSending = false }
        return await perform(failure: "没有记住，请检查网络后再试") {
            try await self.service.tellMemory(userID: self.userID, text: clean)
        }
    }

    func confirm(_ item: MemoryItemPayload) async {
        await perform(failure: "操作失败，请再试一次") {
            try await self.service.confirmMemory(userID: self.userID, itemID: item.id)
        }
    }

    func reject(_ item: MemoryItemPayload) async {
        await perform(failure: "操作失败，请再试一次") {
            try await self.service.rejectMemory(userID: self.userID, itemID: item.id)
        }
    }

    func forget(_ item: MemoryItemPayload) async {
        await perform(failure: "删除失败，请再试一次") {
            try await self.service.deleteMemory(userID: self.userID, itemID: item.id)
        }
    }

    func setPersonalization(_ enabled: Bool) async {
        do {
            memory = try await service.setPersonalization(userID: userID, enabled: enabled)
        } catch {
            toast = Toast(message: "设置没有保存，请再试一次", undoToken: nil)
        }
    }

    func clearAll() async {
        do {
            memory = try await service.clearMemory(userID: userID)
            toast = Toast(message: "已清空全部记忆", undoToken: nil)
        } catch {
            toast = Toast(message: "清空失败，请再试一次", undoToken: nil)
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

    private let examples = ["我是油皮", "我平时穿 42 码", "我一直不买华为", "我一般预算 300 到 500", "我对酒精过敏"]

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
            .navigationTitle("记忆")
            .overlay(alignment: .bottom) { toastView }
            .task { await preferenceStore.load() }
            .onChange(of: isVisible) { _, visible in
                if visible { Task { await preferenceStore.load() } }
            }
            .confirmationDialog(
                "清空全部记忆？",
                isPresented: $showClearConfirm,
                titleVisibility: .visible
            ) {
                Button("清空，不可恢复", role: .destructive) {
                    Task { await preferenceStore.clearAll() }
                }
                Button("取消", role: .cancel) {}
            } message: {
                Text("导购会忘掉你的偏好和最近的行为记录。")
            }
        }
    }

    // MARK: 概览

    /// 标题下的一句话。具体记了什么下面都列着，这里不再重复。
    private func overview(_ memory: MemoryViewPayload) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(memory.count == 0 ? "导购还不了解你" : "导购记得 \(memory.count) 件关于你的事")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
            if !memory.personalizationEnabled {
                Label("推荐暂时不参考这些记忆", systemImage: "pause.circle")
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
                TextField("告诉导购一件事", text: $draft, axis: .vertical)
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
                .accessibilityLabel("告诉导购")
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
            sectionTitle("要记住这些吗？")
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
                    Text(item.source == "inferred" ? evidence : "你说过「\(evidence)」")
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("确认后：\(item.effect)")
                    .font(.caption)
                    .foregroundStyle(item.needsCare ? AppTheme.error : AppTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("记住") { Task { await preferenceStore.confirm(item) } }
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 20)
                        .frame(minHeight: 36)
                        .background(AppTheme.primary, in: Capsule())
                    Button("不对") { Task { await preferenceStore.reject(item) } }
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
            .accessibilityLabel("忘掉 \(item.label)")
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
            return "你说过「\(evidence)」"
        }
        if let evidence = item.evidence, !evidence.isEmpty {
            return "\(item.sourceLabel)：\(evidence)"
        }
        return item.sourceLabel
    }

    private func rowEffect(_ item: MemoryItemPayload) -> String {
        item.useCount > 0 ? "\(item.effect) · 已用于 \(item.useCount) 次推荐" : item.effect
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
                Text("不用填表")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                Text("聊天时说到肤质、尺码、不买的品牌，导购会记下来，也可以直接在上面告诉它。一次性的需求不会被记住。")
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
                        Text("最近的行为")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                        Text("导购据此猜你的偏好，猜到的会先问你")
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
                    Text("推荐时用这些记忆")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(AppTheme.textPrimary)
                    Text(memory.personalizationEnabled ? "关闭后记忆还在，只是暂时不起作用" : "现在的推荐不参考任何记忆")
                        .font(.caption)
                        .foregroundStyle(AppTheme.textSecondary)
                }
            }
            .tint(AppTheme.primary)
            .padding(14)

            if memory.count > 0 || !memory.activity.isEmpty {
                Divider().padding(.leading, 14)
                Button { showClearConfirm = true } label: {
                    Text("清空全部记忆")
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
            Text("连不上服务，暂时看不到记忆")
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
            Button("重试") { Task { await preferenceStore.load() } }
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
                    Button("撤销") {
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
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.unitsStyle = .short
        return formatter
    }()

    static func relative(from raw: String, now: Date = Date()) -> String? {
        guard let date = parser.date(from: raw) else { return nil }
        if now.timeIntervalSince(date) < 60 { return "刚刚" }
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
        summary: "肤质：油皮、不买 华为",
        groups: [
            MemoryGroupPayload(name: "关于你", items: [
                MemoryItemPayload(id: "1", kind: "attribute", group: "关于你", label: "肤质：油皮", scope: "美妆护肤", source: "stated", sourceLabel: "你在对话里说的", evidence: "我是油皮，用什么洗面奶好", effect: "解说时结合", needsCare: false, useCount: 0),
            ]),
            MemoryGroupPayload(name: "品牌", items: [
                MemoryItemPayload(id: "2", kind: "brand_avoid", group: "品牌", label: "不买 华为", scope: nil, source: "stated", sourceLabel: "你在对话里说的", evidence: "我一直不买华为", effect: "硬性排除，不会再推荐", needsCare: false, useCount: 3),
            ]),
        ],
        suggestions: [
            MemoryItemPayload(id: "3", kind: "avoid_term", group: "关于你", label: "避开 酒精", scope: nil, source: "stated", sourceLabel: "你在对话里说的", evidence: "我对酒精过敏", effect: "硬性排除，含这个成分的不推荐", needsCare: true, useCount: 0),
            MemoryItemPayload(id: "4", kind: "brand_like", group: "品牌", label: "喜欢 小米", scope: nil, source: "inferred", sourceLabel: "从你的行为推测", evidence: "最近加购 2 次、看详情 3 次", effect: "排序时优先", needsCare: false, useCount: 0),
        ],
        activity: [
            MemoryActivityPayload(id: "1", type: "add_cart", text: "加购了 小米 17 Max", at: ""),
        ],
        count: 2
    )
}

#Preview("记忆") {
    PreferenceView(preferenceStore: PreferenceStore(service: MockPreferenceService()))
}
