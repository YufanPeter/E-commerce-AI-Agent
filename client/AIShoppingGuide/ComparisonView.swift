import SwiftUI

/// 商品对比页：用两个下拉菜单（仿苹果官网机型选择器）从候选商品里各挑 1 件，
/// 下方渲染结构化对比表（维度行 + 更优项高亮）与选购建议。固定对比 2 件。
///
/// 候选商品来自对话里这条 AI 消息推荐的列表（点「对比商品」小按钮带入）。
/// 数据来自后端 `/compare`（与对话里的 compare 工具复用同一套逻辑）。
struct ComparisonView: View {
    /// 候选商品（当前推荐列表），供下拉菜单选择对比对象。
    let candidates: [Product]

    private let productService = RESTProductService()

    /// 左/右两个对比槽位选中的商品 id；nil 表示未选。默认预选前两件。
    @State private var leftID: String?
    @State private var rightID: String?
    @State private var comparison: ProductComparisonPayload?
    @State private var isLoading = false
    @State private var errorText: String?

    init(candidates: [Product]) {
        self.candidates = candidates
        _leftID = State(initialValue: candidates.count > 0 ? candidates[0].id : nil)
        _rightID = State(initialValue: candidates.count > 1 ? candidates[1].id : nil)
    }

    /// 两个槽位是否已选好且互不相同。
    private var canCompare: Bool {
        guard let l = leftID, let r = rightID else { return false }
        return l != r
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                comparisonSetupPanel

                if isLoading {
                    loadingState
                } else if let comparison {
                    ComparisonTable(comparison: comparison)
                    if !comparison.recommendation.isEmpty {
                        recommendationCard(comparison.recommendation)
                    }
                } else if let errorText {
                    messageCard(errorText, isError: true)
                } else {
                    messageCard("从上方下拉菜单选择两件商品，点「开始对比」～")
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 18)
        }
        .background(AppTheme.background)
        .navigationTitle("商品对比")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - 下拉选择器（仿苹果机型选择器，固定两件）

    private var comparisonSetupPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            slotPickers

            Button(action: { Task { await loadComparison() } }) {
                Text(isLoading ? "对比中…" : "开始对比")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(
                        canCompare ? AppTheme.primary : AppTheme.textSecondary.opacity(0.4),
                        in: RoundedRectangle(cornerRadius: 14, style: .continuous)
                    )
            }
            .buttonStyle(.tactile)
            .disabled(!canCompare || isLoading)
        }
        .padding(16)
        .surfacePanel(cornerRadius: 22)
    }

    private var slotPickers: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("选择对比的两件商品")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.textPrimary)

            slotPicker(selection: $leftID, label: "商品 A")
            slotPicker(selection: $rightID, label: "商品 B")
        }
    }

    private func slotPicker(selection: Binding<String?>, label: String) -> some View {
        let currentID = selection.wrappedValue
        let currentTitle = candidates.first { $0.id == currentID }?.title ?? "请选择\(label)"
        return Menu {
            ForEach(candidates) { product in
                Button {
                    selection.wrappedValue = product.id
                } label: {
                    if currentID == product.id {
                        Label(product.title, systemImage: "checkmark")
                    } else {
                        Text(product.title)
                    }
                }
            }
        } label: {
            HStack {
                Text(currentTitle)
                    .font(.subheadline)
                    .foregroundStyle(currentID == nil ? AppTheme.textSecondary : AppTheme.textPrimary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(AppTheme.textSecondary)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(AppTheme.border, lineWidth: 1)
            )
        }
        .buttonStyle(.tactile)
    }

    // MARK: - 状态视图

    private var loadingState: some View {
        HStack(spacing: 10) {
            ProgressView().tint(AppTheme.primary)
            Text("正在对比…").font(.subheadline).foregroundStyle(AppTheme.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .center)
        .padding(.vertical, 40)
    }

    private func recommendationCard(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("选购建议", systemImage: "checkmark.seal")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.primary)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .surfacePanel(cornerRadius: 18)
    }

    private func messageCard(_ text: String, isError: Bool = false) -> some View {
        Text(text)
            .font(.subheadline)
            .foregroundStyle(isError ? AppTheme.error : AppTheme.textSecondary)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 40)
    }

    private func loadComparison() async {
        guard let l = leftID, let r = rightID, l != r else {
            errorText = "请选择两件不同的商品。"
            return
        }
        isLoading = true
        errorText = nil
        defer { isLoading = false }
        do {
            let request = ProductComparisonRequest(productIDs: [l, r], focus: nil)
            comparison = try await productService.compareProducts(request)
        } catch {
            comparison = nil
            errorText = "对比加载失败，请稍后重试。"
        }
    }
}

/// 可复用的对比表：商品表头 + 维度行（更优项高亮）。
/// 对话流里的对比卡片与对比页都用它，保证渲染一致。
struct ComparisonTable: View {
    let comparison: ProductComparisonPayload

    private let labelWidth: CGFloat = 60

    var body: some View {
        VStack(spacing: 0) {
            header
            ForEach(Array(comparison.rows.enumerated()), id: \.element.id) { index, row in
                rowView(row, zebra: index % 2 == 1)
            }
        }
        .surfacePanel(cornerRadius: 18)
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 0) {
            Color.clear.frame(width: labelWidth)
            ForEach(comparison.products) { product in
                VStack(spacing: 6) {
                    ProductRemoteImage(url: product.imageURL, cornerRadius: 10, placeholderIcon: "shippingbox", contentMode: .fit)
                        .frame(height: 64)
                    Text(product.title)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineLimit(2)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(8)
            }
        }
        .padding(.top, 10)
        .background(AppTheme.secondary.opacity(0.65))
    }

    private func rowView(_ row: ComparisonRowPayload, zebra: Bool) -> some View {
        HStack(alignment: .top, spacing: 0) {
            Text(row.label)
                .font(.caption.weight(.semibold))
                .foregroundStyle(AppTheme.textSecondary)
                .frame(width: labelWidth, alignment: .leading)
                .padding(.vertical, 10)
                .padding(.leading, 10)

            ForEach(Array(row.values.enumerated()), id: \.offset) { index, value in
                let isBest = row.highlight == index
                Text(value)
                    .font(.caption)
                    .foregroundStyle(isBest ? AppTheme.primary : AppTheme.textPrimary)
                    .fontWeight(isBest ? .semibold : .regular)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 10)
                    .overlay(alignment: .topTrailing) {
                        if isBest {
                            Image(systemName: "crown.fill")
                                .font(.system(size: 9))
                                .foregroundStyle(AppTheme.primary)
                                .padding(4)
                        }
                    }
            }
        }
        .background(zebra ? AppTheme.softBlue.opacity(0.42) : Color.clear)
    }
}

/// 对话流里的紧凑对比卡片：直接复用对比表 + 一句建议。
struct ComparisonCard: View {
    let comparison: ProductComparisonPayload

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ComparisonTable(comparison: comparison)
            if !comparison.recommendation.isEmpty {
                Text(comparison.recommendation)
                    .font(.footnote)
                    .foregroundStyle(AppTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: 340, alignment: .leading)
    }
}
