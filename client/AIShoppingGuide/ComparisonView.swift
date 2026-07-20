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

    // MARK: - Two-product pickers

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

    // MARK: - State views

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

/// Reusable comparison table shared by the conversation card and full screen.
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

/// Compact conversation comparison card that reuses the shared table.
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
