import SwiftUI

struct ProductDetailView: View {
    let product: Product
    let onAddToCart: (Product, [String: String], Int) -> Void

    @State private var selectedOptions: [String: String]
    @State private var quantity = 1
    @State private var showSpecificationSheet = false
    @State private var specificationSheetDetent: PresentationDetent = .large
    @State private var showSuccessToast = false
    @State private var toastID = UUID()
    @State private var successQuantity = 1
    @State private var successSummary = ""

    private var toastBottomPadding: CGFloat {
        AppTheme.bottomTabBarHeight + AppTheme.bottomTabBarBottomPadding + 40
    }

    init(
        product: Product,
        onAddToCart: @escaping (Product, [String: String], Int) -> Void
    ) {
        self.product = product
        self.onAddToCart = onAddToCart
        _selectedOptions = State(initialValue: product.defaultSpecificationSelection)
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    RoundedRectangle(cornerRadius: 26, style: .continuous)
                        .fill(LinearGradient(colors: [AppTheme.softPurple, AppTheme.softBlue], startPoint: .topLeading, endPoint: .bottomTrailing))
                        .frame(height: 280)
                        .overlay {
                            VStack(spacing: 12) {
                                Image(systemName: "bag")
                                    .font(.system(size: 54))
                                    .foregroundStyle(AppTheme.primary)
                                Text("商品图占位")
                                    .font(.caption)
                                    .foregroundStyle(AppTheme.textSecondary)
                            }
                        }

                    VStack(alignment: .leading, spacing: 10) {
                        Text(product.price)
                            .font(.title.bold())
                            .foregroundStyle(AppTheme.error)
                        Text(product.title)
                            .font(.title3.bold())
                            .foregroundStyle(AppTheme.textPrimary)
                        Text(product.details)
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.textSecondary)
                            .lineSpacing(4)
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Text("AI 推荐理由")
                            .font(.headline)
                        Text(product.reason)
                            .font(.subheadline)
                            .foregroundStyle(AppTheme.textSecondary)
                    }
                    .padding(16)
                    .floatingLiquidPanel(cornerRadius: 20)

                    HStack {
                        ForEach(product.tags, id: \.self) { tag in
                            Text(tag)
                                .font(.caption.weight(.medium))
                                .foregroundStyle(AppTheme.primary)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 7)
                                .background(AppTheme.softPurple, in: Capsule())
                        }
                    }

                    Button {
                        openSpecificationSheet()
                    } label: {
                        Text("加入购物车")
                            .font(.headline)
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 15)
                            .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    }
                    .padding(.top, 8)
                }
                .padding(20)
                .padding(.bottom, 112)
            }

            if showSuccessToast {
                AddToCartSuccessToast(quantity: successQuantity, summary: successSummary)
                    .padding(.horizontal, 20)
                    .padding(.bottom, toastBottomPadding)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .sheet(isPresented: $showSpecificationSheet) {
            AddToCartSheet(
                product: product,
                selectedOptions: $selectedOptions,
                quantity: $quantity,
                onConfirm: confirmAddToCart
            )
            .presentationDetents([.medium, .large], selection: $specificationSheetDetent)
            .presentationDragIndicator(.visible)
            .presentationCornerRadius(28)
        }
        .background(AppTheme.background)
        .navigationTitle("商品详情")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var selectedSpecificationSummary: String {
        selectedOptions
            .sorted { $0.key < $1.key }
            .map { "\($0.key)：\($0.value)" }
            .joined(separator: "  ")
    }

    private func openSpecificationSheet() {
        specificationSheetDetent = .large
        showSpecificationSheet = true
    }

    private func confirmAddToCart() {
        onAddToCart(product, selectedOptions, quantity)
        successQuantity = quantity
        successSummary = selectedSpecificationSummary
        showSpecificationSheet = false
        toastID = UUID()
        let currentToastID = toastID

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.16) {
            withAnimation(.spring(response: 0.28, dampingFraction: 0.82)) {
                showSuccessToast = true
            }
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            guard currentToastID == toastID else { return }
            withAnimation(.easeOut(duration: 0.18)) {
                showSuccessToast = false
            }
        }
    }
}

private struct AddToCartSheet: View {
    let product: Product
    @Binding var selectedOptions: [String: String]
    @Binding var quantity: Int
    let onConfirm: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    specificationControls
                    quantityControl
                }
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 112)
            }
            .background(AppTheme.background)
            .safeAreaInset(edge: .bottom) {
                Button(action: onConfirm) {
                    Text("确认加入购物车")
                        .font(.headline)
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 15)
                        .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .padding(.bottom, 12)
                .background(.regularMaterial)
            }
            .navigationTitle("选择规格")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(LinearGradient(colors: [AppTheme.softPurple, AppTheme.softBlue], startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 76, height: 76)
                .overlay {
                    Image(systemName: "bag")
                        .font(.system(size: 28, weight: .semibold))
                        .foregroundStyle(AppTheme.primary)
                }

            VStack(alignment: .leading, spacing: 6) {
                Text(product.price)
                    .font(.title3.bold())
                    .foregroundStyle(AppTheme.error)
                Text(product.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(2)
                Text(selectedSpecificationSummary)
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(1)
            }
        }
        .padding(14)
        .floatingLiquidPanel(cornerRadius: 20)
    }

    private var specificationControls: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(product.specifications) { specification in
                VStack(alignment: .leading, spacing: 10) {
                    Text(specification.name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)

                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 82), spacing: 10)], spacing: 10) {
                        ForEach(specification.options, id: \.self) { option in
                            SpecificationOptionButton(
                                title: option,
                                isSelected: selectedOptions[specification.name] == option
                            ) {
                                selectedOptions[specification.name] = option
                            }
                        }
                    }
                }
            }
        }
        .padding(16)
        .floatingLiquidPanel(cornerRadius: 20)
    }

    private var quantityControl: some View {
        HStack(alignment: .center) {
            Text("加购数量")
                .font(.headline)
            Spacer()
            QuantityControl(quantity: $quantity)
        }
        .padding(16)
        .floatingLiquidPanel(cornerRadius: 20)
    }

    private var selectedSpecificationSummary: String {
        selectedOptions
            .sorted { $0.key < $1.key }
            .map { "\($0.key)：\($0.value)" }
            .joined(separator: "  ")
    }
}

private struct SpecificationOptionButton: View {
    let title: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(isSelected ? AppTheme.primary : AppTheme.textPrimary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .padding(.horizontal, 8)
                .background(
                    isSelected ? AppTheme.primary.opacity(0.12) : AppTheme.surface,
                    in: RoundedRectangle(cornerRadius: 12, style: .continuous)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .stroke(isSelected ? AppTheme.primary : AppTheme.border, lineWidth: isSelected ? 1.2 : 0.7)
                )
        }
        .buttonStyle(.plain)
    }
}

private struct AddToCartSuccessToast: View {
    let quantity: Int
    let summary: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 24, weight: .semibold))
                .foregroundStyle(AppTheme.success)

            VStack(alignment: .leading, spacing: 3) {
                Text("已加入购物车")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                Text(detailText)
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(1)
            }

            Spacer(minLength: 0)
        }
        .padding(14)
        .floatingLiquidPanel(cornerRadius: 20)
    }

    private var detailText: String {
        let quantityText = "x\(quantity)"
        guard !summary.isEmpty else { return quantityText }
        return "\(summary)  \(quantityText)"
    }
}
