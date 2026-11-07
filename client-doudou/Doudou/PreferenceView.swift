import SwiftUI

@MainActor
final class PreferenceStore: ObservableObject {
    @Published private(set) var preference: UserPreferencePayload = .empty()
    @Published private(set) var isLoading = false
    @Published private(set) var syncMessage: String?

    private let service: any PreferenceServicing
    private let userID: String
    private var saveTask: Task<Void, Never>?

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
            preference = try await service.fetchPreference(userID: userID)
            syncMessage = nil
        } catch {
            syncMessage = "偏好暂时无法同步"
        }
    }

    func applyMemoryUpdate(_ update: MemoryUpdatePayload) {
        if let remote = update.preference {
            preference = remote
        }
        syncMessage = update.message
    }

    func undo(_ update: MemoryUpdatePayload) async {
        do {
            preference = try await service.undoPreference(userID: preference.userID, undoToken: update.undoToken)
            syncMessage = "已撤销刚才记住的偏好"
        } catch {
            syncMessage = "撤销失败"
        }
    }

    func setBudget(min: Double?, max: Double?) {
        mutate {
            $0.budgetMin = min
            $0.budgetMax = max
        }
    }

    func setPriceTier(_ value: String) {
        mutate { $0.priceTier = value }
    }

    func setPersonalizationEnabled(_ enabled: Bool) {
        mutate { $0.personalizationEnabled = enabled }
    }

    func toggleCategory(_ value: String) {
        mutate { toggle(value, in: &$0.favoriteCategories) }
    }

    func toggleStyle(_ value: String) {
        mutate { toggle(value, in: &$0.preferenceKeywords) }
    }

    func addKeyword(_ value: String) {
        let clean = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        mutate {
            if !$0.preferenceKeywords.contains(clean) {
                $0.preferenceKeywords.append(clean)
            }
        }
    }

    func updateKeyword(_ oldValue: String, to newValue: String) {
        let clean = newValue.trimmingCharacters(in: .whitespacesAndNewlines)
        mutate {
            $0.preferenceKeywords.removeAll { $0 == oldValue }
            if !clean.isEmpty && !$0.preferenceKeywords.contains(clean) {
                $0.preferenceKeywords.append(clean)
            }
        }
    }

    func removeKeyword(_ value: String) {
        mutate { $0.preferenceKeywords.removeAll { $0 == value } }
    }

    func addBrand(_ value: String, to field: BrandField) {
        let clean = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        mutate {
            switch field {
            case .include:
                if !$0.brandInclude.contains(clean) { $0.brandInclude.append(clean) }
            case .exclude:
                if !$0.brandExclude.contains(clean) { $0.brandExclude.append(clean) }
            }
        }
    }

    func removeBrand(_ value: String, from field: BrandField) {
        mutate {
            switch field {
            case .include:
                $0.brandInclude.removeAll { $0 == value }
            case .exclude:
                $0.brandExclude.removeAll { $0 == value }
            }
        }
    }

    func setNotes(_ value: String) {
        mutate {
            let clean = value.trimmingCharacters(in: .whitespacesAndNewlines)
            $0.preferenceNote = clean.isEmpty ? nil : value
            $0.notes = $0.preferenceNote
        }
    }

    private func mutate(_ edit: (inout UserPreferencePayload) -> Void) {
        var next = preference
        edit(&next)
        preference = next
        scheduleSave(next)
    }

    private func scheduleSave(_ value: UserPreferencePayload) {
        saveTask?.cancel()
        saveTask = Task { [service] in
            try? await Task.sleep(nanoseconds: 900_000_000)
            guard !Task.isCancelled else { return }
            do {
                let saved = try await service.updatePreference(value)
                await MainActor.run {
                    self.preference = saved
                    self.syncMessage = "偏好已同步"
                }
            } catch {
                await MainActor.run { self.syncMessage = "偏好未同步" }
            }
        }
    }

    private func toggle(_ value: String, in array: inout [String]) {
        if array.contains(value) {
            array.removeAll { $0 == value }
        } else {
            array.append(value)
        }
    }

    enum BrandField {
        case include
        case exclude
    }
}

struct PreferenceView: View {
    @ObservedObject var preferenceStore: PreferenceStore
    @State private var includeBrandText = ""
    @State private var excludeBrandText = ""
    @State private var keywordText = ""
    @State private var editingKeyword: String?
    @State private var editingKeywordText = ""
    @State private var notesDraft = ""

    private let categories = ["美妆护肤", "数码电子", "服饰运动", "食品生活"]
    private let recommendedKeywords = ["轻量", "通勤", "高性价比", "品质优先", "便携", "送礼", "环保", "耐用"]
    private let tiers = [("value", "省心价"), ("balanced", "均衡"), ("premium", "品质")]

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 18) {
                    profileCard
                    budgetCard
                    categoryCard
                    brandCard
                    keywordCard
                    notesCard
                }
                .padding(20)
                .padding(.bottom, 104)
            }
            .background(AppTheme.background)
            .navigationTitle("个人偏好")
            .task { await preferenceStore.load() }
            .onChange(of: preferenceStore.preference.preferenceNote ?? preferenceStore.preference.notes ?? "") { _, newValue in
                notesDraft = newValue
            }
        }
    }

    private var profileCard: some View {
        HStack(spacing: 14) {
            Circle()
                .fill(AppTheme.secondary)
                .frame(width: 62, height: 62)
                .overlay(Text("L").font(.title2.bold()).foregroundStyle(.white))
            VStack(alignment: .leading, spacing: 4) {
                Text("Lily")
                    .font(.title3.bold())
                Text(preferenceStore.syncMessage ?? "偏好已同步")
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
            }
            Spacer()
            Toggle("", isOn: Binding(
                get: { preferenceStore.preference.personalizationEnabled },
                set: { preferenceStore.setPersonalizationEnabled($0) }
            ))
            .labelsHidden()
            .tint(AppTheme.primary)
            if preferenceStore.isLoading {
                ProgressView().tint(AppTheme.primary)
            }
        }
        .padding(16)
        .surfacePanel(cornerRadius: 16)
    }

    private var budgetCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("预算与价格")
                .font(.headline)
            HStack(spacing: 10) {
                budgetField("最低", value: preferenceStore.preference.budgetMin) { minValue in
                    preferenceStore.setBudget(min: minValue, max: preferenceStore.preference.budgetMax)
                }
                budgetField("最高", value: preferenceStore.preference.budgetMax) { maxValue in
                    preferenceStore.setBudget(min: preferenceStore.preference.budgetMin, max: maxValue)
                }
            }
            Picker("价格倾向", selection: Binding(
                get: { preferenceStore.preference.priceTier ?? "balanced" },
                set: { preferenceStore.setPriceTier($0) }
            )) {
                ForEach(tiers, id: \.0) { tier in
                    Text(tier.1).tag(tier.0)
                }
            }
            .pickerStyle(.segmented)
        }
        .padding(16)
        .surfacePanel(cornerRadius: 16)
    }

    private func budgetField(
        _ title: String,
        value: Double?,
        onCommit: @escaping (Double?) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(AppTheme.textSecondary)
            TextField("不限", text: Binding(
                get: { value.map { String(Int($0)) } ?? "" },
                set: { onCommit(Double($0)) }
            ))
            .keyboardType(.numberPad)
            .textFieldStyle(.roundedBorder)
        }
    }

    private var categoryCard: some View {
        chipCard(title: "关注品类", values: categories, selected: preferenceStore.preference.favoriteCategories) {
            preferenceStore.toggleCategory($0)
        }
    }

    private var keywordCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("购物风格").font(.headline)
            EditableKeywordGrid(
                keywords: preferenceStore.preference.preferenceKeywords,
                editingKeyword: $editingKeyword,
                editingText: $editingKeywordText,
                onSave: { oldValue, newValue in preferenceStore.updateKeyword(oldValue, to: newValue) },
                onDelete: { preferenceStore.removeKeyword($0) }
            )
            HStack {
                TextField("新增关键词", text: $keywordText)
                    .textFieldStyle(.roundedBorder)
                Button {
                    preferenceStore.addKeyword(keywordText)
                    keywordText = ""
                } label: {
                    Image(systemName: "plus.circle.fill")
                }
                .buttonStyle(.plain)
                .foregroundStyle(AppTheme.primary)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("推荐关键词")
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 88), spacing: 10)], spacing: 10) {
                    ForEach(recommendedKeywords.filter { !preferenceStore.preference.preferenceKeywords.contains($0) }, id: \.self) { value in
                        Button { preferenceStore.addKeyword(value) } label: {
                            Text(value)
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(AppTheme.textPrimary)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 10)
                                .background(AppTheme.secondary.opacity(0.24), in: Capsule())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(16)
        .surfacePanel(cornerRadius: 16)
    }

    private func chipCard(
        title: String,
        values: [String],
        selected: [String],
        onTap: @escaping (String) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title).font(.headline)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 88), spacing: 10)], spacing: 10) {
                ForEach(values, id: \.self) { value in
                    Button { onTap(value) } label: {
                        Text(value)
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(selected.contains(value) ? .white : AppTheme.textPrimary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 10)
                            .background(selected.contains(value) ? AppTheme.primary : AppTheme.secondary.opacity(0.38), in: Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(16)
        .surfacePanel(cornerRadius: 16)
    }

    private var brandCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("品牌偏好").font(.headline)
            brandEditor(
                title: "喜欢品牌",
                text: $includeBrandText,
                values: preferenceStore.preference.brandInclude,
                field: .include
            )
            brandEditor(
                title: "避免品牌",
                text: $excludeBrandText,
                values: preferenceStore.preference.brandExclude,
                field: .exclude
            )
        }
        .padding(16)
        .surfacePanel(cornerRadius: 16)
    }

    private func brandEditor(
        title: String,
        text: Binding<String>,
        values: [String],
        field: PreferenceStore.BrandField
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.subheadline.weight(.medium))
            HStack {
                TextField("输入品牌", text: text)
                    .textFieldStyle(.roundedBorder)
                Button {
                    preferenceStore.addBrand(text.wrappedValue, to: field)
                    text.wrappedValue = ""
                } label: {
                    Image(systemName: "plus.circle.fill")
                }
                .buttonStyle(.plain)
                .foregroundStyle(AppTheme.primary)
            }
            FlowTagsEditable(tags: values) { tag in
                preferenceStore.removeBrand(tag, from: field)
            }
        }
    }

    private var notesCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("补充偏好").font(.headline)
            TextEditor(text: Binding(
                get: { notesDraft.isEmpty ? (preferenceStore.preference.preferenceNote ?? preferenceStore.preference.notes ?? "") : notesDraft },
                set: {
                    notesDraft = $0
                    preferenceStore.setNotes($0)
                }
            ))
            .frame(minHeight: 90)
            .scrollContentBackground(.hidden)
            .padding(8)
            .background(.white.opacity(0.7), in: RoundedRectangle(cornerRadius: 8))
        }
        .padding(16)
        .surfacePanel(cornerRadius: 16)
    }
}

struct FlowTagsEditable: View {
    let tags: [String]
    let onRemove: (String) -> Void

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 78), spacing: 10)], spacing: 10) {
            ForEach(tags, id: \.self) { tag in
                Button { onRemove(tag) } label: {
                    HStack(spacing: 4) {
                        Text(tag).lineLimit(1)
                        Image(systemName: "xmark.circle.fill").font(.caption)
                    }
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(AppTheme.textPrimary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(AppTheme.secondary.opacity(0.38), in: Capsule())
                }
                .buttonStyle(.plain)
            }
        }
    }
}

struct EditableKeywordGrid: View {
    let keywords: [String]
    @Binding var editingKeyword: String?
    @Binding var editingText: String
    let onSave: (String, String) -> Void
    let onDelete: (String) -> Void

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 96), spacing: 12)], spacing: 12) {
            ForEach(keywords, id: \.self) { keyword in
                if editingKeyword == keyword {
                    HStack(spacing: 4) {
                        TextField("关键词", text: $editingText)
                            .textFieldStyle(.roundedBorder)
                        Button {
                            onSave(keyword, editingText)
                            editingKeyword = nil
                            editingText = ""
                        } label: {
                            Image(systemName: "checkmark.circle.fill")
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(AppTheme.primary)
                    }
                } else {
                    Menu {
                        Button("编辑") {
                            editingKeyword = keyword
                            editingText = keyword
                        }
                        Button("删除", role: .destructive) {
                            onDelete(keyword)
                        }
                    } label: {
                        Text(keyword)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 12)
                            .background(AppTheme.secondary.opacity(0.24), in: Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}
