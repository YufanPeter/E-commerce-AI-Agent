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
        stickyButtonBottomPadding + 74
    }

    private var stickyButtonBottomPadding: CGFloat {
        AppTheme.bottomTabBarHeight + AppTheme.bottomTabBarBottomPadding + AppTheme.guideComposerTabGap
    }

    private var bottomOverlayHeight: CGFloat {
        stickyButtonBottomPadding + 84
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
                    productHeroImage
                    productSummaryPanel

                    if !product.reviews.isEmpty {
                        ProductReviewsSection(reviews: product.reviews)
                    }
                }
                .padding(20)
                .padding(.top, 24)
                .padding(.bottom, bottomOverlayHeight + 20)
            }

            if showSuccessToast {
                AddToCartSuccessToast(quantity: successQuantity, summary: successSummary)
                    .padding(.horizontal, 20)
                    .padding(.bottom, toastBottomPadding)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            detailBottomScrim
            stickyAddToCartButton
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
        .toolbarBackground(AppTheme.background, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
    }

    private var stickyAddToCartButton: some View {
        Button {
            openSpecificationSheet()
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "cart.badge.plus")
                    .font(.headline.weight(.semibold))
                Text("加入购物车")
                    .font(.headline)
            }
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 15)
            .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .shadow(color: AppTheme.accentGlow, radius: 14, y: 5)
        }
        .buttonStyle(.tactile)
        .padding(.horizontal, 20)
        .padding(.bottom, stickyButtonBottomPadding)
    }

    private var detailBottomScrim: some View {
        Rectangle()
            .fill(.ultraThinMaterial)
            .frame(height: bottomOverlayHeight)
            .mask(
                LinearGradient(
                    colors: [.clear, .black.opacity(0.42), .black.opacity(0.82)],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
            .allowsHitTesting(false)
            .ignoresSafeArea(edges: .bottom)
    }

    private var productHeroImage: some View {
        GeometryReader { geometry in
            ProductRemoteImage(url: product.imageURL, cornerRadius: 24, placeholderIcon: "bag", contentMode: .fit)
                .frame(width: geometry.size.width, height: geometry.size.width)
                .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 24, style: .continuous)
                        .stroke(AppTheme.border, lineWidth: 1)
                )
                .shadow(color: AppTheme.shadow.opacity(0.42), radius: 16, y: 8)
        }
        .aspectRatio(1, contentMode: .fit)
    }

    private var productSummaryPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            productInfo

            if !product.reason.isEmpty {
                productTextBlock(title: "推荐依据", text: product.reason, icon: "sparkles")
            }

            if !product.details.isEmpty {
                productTextBlock(title: "商品资料", text: product.details, icon: "doc.text")
            }

            if !product.tags.isEmpty {
                productTags
            }
        }
        .padding(16)
        .surfacePanel(cornerRadius: 22)
    }

    private var productInfo: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(product.priceDisplay(for: selectedOptions))
                .font(.system(size: 32, weight: .bold))
                .foregroundStyle(AppTheme.error)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
            Text(product.title)
                .font(.title2.bold())
                .foregroundStyle(AppTheme.textPrimary)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func productTextBlock(title: String, text: String, icon: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Label(title, systemImage: icon)
                .font(.caption.weight(.semibold))
                .foregroundStyle(AppTheme.primary)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
                .lineSpacing(4)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.secondary.opacity(0.48), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var productTags: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 82), spacing: 8, alignment: .leading)], alignment: .leading, spacing: 8) {
            ForEach(product.tags, id: \.self) { tag in
                Text(tag)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.82)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(AppTheme.secondary.opacity(0.78), in: Capsule())
            }
        }
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

private struct ProductReviewsSection: View {
    let reviews: [ProductReview]
    @State private var isExpanded = false

    private var visibleReviews: [ProductReview] {
        isExpanded ? reviews : Array(reviews.prefix(3))
    }

    private var hiddenReviewCount: Int {
        max(0, reviews.count - 3)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text("用户评价")
                    .font(.headline)
                    .foregroundStyle(AppTheme.textPrimary)
                Text("\(reviews.count) 条")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.textSecondary)
            }

            VStack(alignment: .leading, spacing: 12) {
                ForEach(visibleReviews) { review in
                    ProductReviewRow(review: review)
                    if review.id != visibleReviews.last?.id {
                        Divider().overlay(AppTheme.border)
                    }
                }
            }

            if hiddenReviewCount > 0 {
                Button {
                    withAnimation(.easeOut(duration: 0.18)) {
                        isExpanded.toggle()
                    }
                } label: {
                    HStack(spacing: 5) {
                        Text(isExpanded ? "收起评价" : "查看更多 \(hiddenReviewCount) 条")
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .font(.caption.weight(.semibold))
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.primary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 9)
                    .background(AppTheme.softPurple.opacity(0.55), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
                .buttonStyle(.tactile)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .surfacePanel(cornerRadius: 20)
    }
}

private struct ProductReviewRow: View {
    let review: ProductReview

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 10) {
                Text(review.nickname)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.82)
                Spacer(minLength: 8)
                RatingStars(rating: review.rating)
            }

            Text(review.content)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct RatingStars: View {
    let rating: Int

    var body: some View {
        HStack(spacing: 2) {
            ForEach(1...5, id: \.self) { index in
                Image(systemName: index <= rating ? "star.fill" : "star")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(index <= rating ? AppTheme.primary : AppTheme.textSecondary.opacity(0.45))
            }
        }
        .accessibilityLabel("\(rating) 星")
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
                .buttonStyle(.tactile)
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
            ProductRemoteImage(url: product.imageURL, cornerRadius: 16, placeholderIcon: "bag")
                .frame(width: 76, height: 76)

            VStack(alignment: .leading, spacing: 6) {
                Text(product.priceDisplay(for: selectedOptions))
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
        .surfacePanel(cornerRadius: 20)
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
        .surfacePanel(cornerRadius: 20)
    }

    private var quantityControl: some View {
        HStack(alignment: .center) {
            Text("加购数量")
                .font(.headline)
            Spacer()
            QuantityControl(quantity: $quantity)
        }
        .padding(16)
        .surfacePanel(cornerRadius: 20)
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
        .buttonStyle(.tactile)
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
        .surfacePanel(cornerRadius: 20)
    }

    private var detailText: String {
        let quantityText = "x\(quantity)"
        guard !summary.isEmpty else { return quantityText }
        return "\(summary)  \(quantityText)"
    }
}
