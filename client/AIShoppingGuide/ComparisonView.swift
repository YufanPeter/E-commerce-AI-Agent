import SwiftUI

/// Product comparison screen with two candidate pickers and a structured comparison table.
///
/// Candidates come from one assistant recommendation and data comes from `/compare`.
struct ComparisonView: View {
    /// Current recommendation candidates available to both pickers.
    let candidates: [Product]

    private let productService = RESTProductService()

    /// Product IDs selected in the left and right slots; defaults to the first two.
    @State private var leftID: String?
    @State private var rightID: String?
    /// 各商品当前选中的规格（点表头切换）；没选过的由后端按最便宜 / 预算自动定。
    @State private var selectedSKUs: [String: String] = [:]
    @State private var comparison: ProductComparisonPayload?
    @State private var isLoading = false
    @State private var errorText: String?

    init(candidates: [Product]) {
        self.candidates = candidates
        _leftID = State(initialValue: candidates.count > 0 ? candidates[0].id : nil)
        _rightID = State(initialValue: candidates.count > 1 ? candidates[1].id : nil)
    }

    /// Whether both slots contain different products.
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
                    ComparisonTable(comparison: comparison) { productID, skuID in
                        selectedSKUs[productID] = skuID
                        Task { await loadComparison() }
                    }
                    if !comparison.recommendation.isEmpty {
                        recommendationCard(comparison.recommendation)
                    }
                } else if let errorText {
                    messageCard(errorText, isError: true)
                } else {
                    messageCard("Choose two products above, then tap Compare.")
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 18)
        }
        .background(AppTheme.background)
        .navigationTitle("Product comparison")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - Two-product pickers

    private var comparisonSetupPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            slotPickers

            Button(action: { Task { await loadComparison() } }) {
                Text(isLoading ? "Comparing…" : "Compare")
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
        .surfacePanel(cornerRadius: 12)
    }

    private var slotPickers: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Choose two products")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.textPrimary)

            slotPicker(selection: $leftID, label: "Product A")
            slotPicker(selection: $rightID, label: "Product B")
        }
    }

    private func slotPicker(selection: Binding<String?>, label: String) -> some View {
        let currentID = selection.wrappedValue
        let currentTitle = candidates.first { $0.id == currentID }?.title ?? "Choose \(label)"
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

    // MARK: - State views

    private var loadingState: some View {
        HStack(spacing: 10) {
            ProgressView().tint(AppTheme.primary)
            Text("Comparing…").font(.subheadline).foregroundStyle(AppTheme.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .center)
        .padding(.vertical, 40)
    }

    private func recommendationCard(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Buying advice", systemImage: "checkmark.seal")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(AppTheme.primary)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .surfacePanel(cornerRadius: 12)
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
            errorText = "Choose two different products."
            return
        }
        isLoading = true
        errorText = nil
        defer { isLoading = false }
        do {
            var request = ProductComparisonRequest(productIDs: [l, r], focus: nil)
            request.items = [l, r].map { ComparisonItemRequest(productID: $0, skuID: selectedSKUs[$0]) }
            comparison = try await productService.compareProducts(request)
        } catch {
            comparison = nil
            errorText = "Could not load the comparison. Try again."
        }
    }
}

/// Reusable comparison table shared by the conversation card and full screen.
struct ComparisonTable: View {
    let comparison: ProductComparisonPayload
    /// 点表头规格切换；对话流里的静态卡片传 nil（只展示选中的规格）。
    var onSelectSKU: ((String, String) -> Void)? = nil
    /// 只显示前几行，逐行展开时用；nil 显示全部。
    var visibleRowCount: Int? = nil

    private let labelWidth: CGFloat = 60

    init(
        comparison: ProductComparisonPayload,
        visibleRowCount: Int? = nil,
        onSelectSKU: ((String, String) -> Void)? = nil
    ) {
        self.comparison = comparison
        self.visibleRowCount = visibleRowCount
        self.onSelectSKU = onSelectSKU
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            ForEach(Array(comparison.rows.prefix(visibleRowCount ?? comparison.rows.count).enumerated()), id: \.element.id) { index, row in
                rowView(row, zebra: index % 2 == 1)
                    .transition(.opacity)
            }
        }
        .surfacePanel(cornerRadius: 12)
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
                    skuBadge(for: product)
                }
                .frame(maxWidth: .infinity)
                .padding(8)
            }
        }
        .padding(.top, 10)
        .background(AppTheme.secondary.opacity(0.65))
    }

    /// 表头下的规格标签：「12GB+256GB · 星芒黑」。可切换时是一个下拉菜单，列出全部规格与价格。
    @ViewBuilder
    private func skuBadge(for product: ComparisonProductPayload) -> some View {
        if let sku = product.sku, !sku.label.isEmpty {
            let label = HStack(spacing: 3) {
                Text(sku.label)
                    .font(.caption2)
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
                if onSelectSKU != nil, (product.skus?.count ?? 0) > 1 {
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.system(size: 8, weight: .semibold))
                }
            }
            .foregroundStyle(AppTheme.primary)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(AppTheme.surface, in: Capsule())
            .overlay(Capsule().stroke(AppTheme.border, lineWidth: 1))

            if let onSelectSKU, let skus = product.skus, skus.count > 1 {
                Menu {
                    ForEach(skus) { option in
                        Button {
                            onSelectSKU(product.productID, option.skuID)
                        } label: {
                            if option.skuID == sku.skuID {
                                Label("\(option.label)  \(option.priceDisplay)", systemImage: "checkmark")
                            } else {
                                Text("\(option.label)  \(option.priceDisplay)")
                            }
                        }
                    }
                } label: {
                    label
                }
            } else {
                label
            }
        }
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

/// Compact conversation comparison card that reuses the shared table.
struct ComparisonCard: View {
    let comparison: ProductComparisonPayload
    /// 回答生成中逐行展开；历史消息和已完成的回答直接显示完整表格。
    var revealsRows: Bool = false
    /// 模型写了选购建议时就不再显示这句兜底建议。
    var showsRecommendation: Bool = true

    @State private var revealedRows = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var visibleRowCount: Int? {
        revealsRows && !reduceMotion ? revealedRows : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ComparisonTable(comparison: comparison, visibleRowCount: visibleRowCount)
                .task {
                    guard revealsRows else { return }
                    while revealedRows < comparison.rows.count, !Task.isCancelled {
                        try? await Task.sleep(nanoseconds: 160_000_000)
                        withAnimation(.easeOut(duration: 0.2)) {
                            revealedRows += 1
                        }
                    }
                }
            if showsRecommendation, !comparison.recommendation.isEmpty {
                Text(comparison.recommendation)
                    .font(.footnote)
                    .foregroundStyle(AppTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: 340, alignment: .leading)
    }
}
