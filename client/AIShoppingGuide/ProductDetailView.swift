import AVFoundation
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
                    ProductAskSection(product: product)

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
            if product.referenceOnly == true {
                Text("Reference price · Availability unknown")
                    .font(.subheadline)
                    .foregroundStyle(AppTheme.textSecondary)
                    .padding(.bottom, stickyButtonBottomPadding + 12)
            } else {
                stickyAddToCartButton
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
        .navigationTitle("Product details")
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
                Text("Add to cart")
                    .font(.headline)
            }
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 15)
            .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .shadow(color: AppTheme.shadow.opacity(0.45), radius: 7, y: 2)
        }
        .buttonStyle(.tactile)
        .padding(.horizontal, 20)
        .padding(.bottom, stickyButtonBottomPadding)
    }

    private var detailBottomScrim: some View {
        Rectangle()
            .fill(AppTheme.surface.opacity(0.94))
            .frame(height: bottomOverlayHeight)
            .mask(
                LinearGradient(
                    colors: [.clear, .black.opacity(0.22), .black.opacity(0.58)],
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
                productTextBlock(title: "Why this product", text: product.reason, icon: "lightbulb")
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
        ProductTagRow(tags: product.tags)
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

/// 详情页的导购入口：听亮点，以及来自商品问答的常见问题。
private struct ProductAskSection: View {
    let product: Product

    @StateObject private var speaker = HighlightsSpeaker()
    @State private var openFAQ: String?

    private let productService = RESTProductService()

    private var faqs: [ProductFAQ] { product.faqs ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {
                Task { await toggleHighlights() }
            } label: {
                HStack(spacing: 8) {
                    Group {
                        if speaker.state == .loading {
                            ProgressView().controlSize(.small).tint(AppTheme.primary)
                        } else {
                            Image(systemName: speaker.state == .speaking ? "stop.fill" : "speaker.wave.2.fill")
                                .font(.system(size: 12, weight: .semibold))
                        }
                    }
                    .frame(width: 16, height: 16)
                    Text(speaker.state == .speaking ? "Stop audio" : "Listen to highlights")
                        .font(.subheadline.weight(.medium))
                }
                .foregroundStyle(AppTheme.primary)
                .padding(.horizontal, 14)
                .frame(minHeight: 38)
                .background(AppTheme.secondary, in: Capsule())
            }
            .buttonStyle(.tactile)
            .disabled(speaker.state == .loading)

            if !faqs.isEmpty {
                Text("Frequently asked questions")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(AppTheme.textSecondary)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(faqs.enumerated()), id: \.element.id) { index, faq in
                        if index > 0 { Divider() }
                        faqRow(faq)
                    }
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .surfacePanel(cornerRadius: 22)
        .onDisappear { speaker.stop() }
    }

    private func faqRow(_ faq: ProductFAQ) -> some View {
        let isOpen = openFAQ == faq.id
        return VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.easeOut(duration: 0.18)) {
                    openFAQ = isOpen ? nil : faq.id
                }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(faq.question)
                        .font(.subheadline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.down")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(AppTheme.textSecondary)
                        .rotationEffect(.degrees(isOpen ? 180 : 0))
                }
                .padding(.vertical, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if isOpen {
                Text(faq.answer)
                    .font(.footnote)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 10)
                    .transition(.opacity)
            }
        }
    }

    /// 优先念模型写的短卖点，取不到就念商品介绍。
    private func toggleHighlights() async {
        if speaker.state == .speaking {
            speaker.stop()
            return
        }
        speaker.state = .loading
        let pitch = await productService.fetchProductPitch(productID: product.id)
        let text = [pitch, product.reason].compactMap { $0 }.first { !$0.isEmpty } ?? product.title
        speaker.speak(text)
    }
}

/// 用系统语音念亮点。念完或离开页面就释放音频，不影响其他 App 继续播放。
@MainActor
private final class HighlightsSpeaker: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    enum State { case idle, loading, speaking }

    @Published var state: State = .idle

    private let synthesizer = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String) {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio, options: .duckOthers)
        try? AVAudioSession.sharedInstance().setActive(true)
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        state = .speaking
        synthesizer.speak(utterance)
    }

    func stop() {
        synthesizer.stopSpeaking(at: .immediate)
        finish()
    }

    private func finish() {
        state = .idle
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.finish() }
    }
}

private struct ProductReviewsSection: View {
    let reviews: [ProductReview]
    @State private var isExpanded = false
    @State private var selectedRating: Int?
    @State private var sortMode: ReviewSortMode = .defaultOrder

    private var visibleReviews: [ProductReview] {
        isExpanded ? filteredAndSortedReviews : Array(filteredAndSortedReviews.prefix(3))
    }

    private var hiddenReviewCount: Int {
        max(0, filteredAndSortedReviews.count - 3)
    }

    private var filteredAndSortedReviews: [ProductReview] {
        let filtered = reviews.enumerated().filter { _, review in
            selectedRating == nil || review.rating == selectedRating
        }

        let sorted: [(offset: Int, element: ProductReview)]
        switch sortMode {
        case .defaultOrder:
            sorted = filtered.sorted { $0.offset < $1.offset }
        case .ratingHighToLow:
            sorted = filtered.sorted {
                if $0.element.rating == $1.element.rating {
                    return $0.offset < $1.offset
                }
                return $0.element.rating > $1.element.rating
            }
        case .ratingLowToHigh:
            sorted = filtered.sorted {
                if $0.element.rating == $1.element.rating {
                    return $0.offset < $1.offset
                }
                return $0.element.rating < $1.element.rating
            }
        }
        return sorted.map(\.element)
    }

    private var availableRatings: [Int] {
        Array(Set(reviews.map(\.rating))).sorted(by: >)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text("Customer reviews")
                    .font(.headline)
                    .foregroundStyle(AppTheme.textPrimary)
                Text("\(filteredAndSortedReviews.count) of \(reviews.count)")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(AppTheme.textSecondary)
                Spacer(minLength: 12)
                ratingMenu
                sortMenu
            }

            if visibleReviews.isEmpty {
                Text("No reviews match this filter.")
                    .font(.subheadline)
                    .foregroundStyle(AppTheme.textSecondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 10)
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(visibleReviews) { review in
                        ProductReviewRow(review: review)
                        if review.id != visibleReviews.last?.id {
                            Divider().overlay(AppTheme.border)
                        }
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
                        Text(isExpanded ? "Show fewer reviews" : "Show \(hiddenReviewCount) more reviews")
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
        .onChange(of: selectedRating) { _, _ in
            withAnimation(.easeOut(duration: 0.18)) {
                isExpanded = false
            }
        }
        .onChange(of: sortMode) { _, _ in
            withAnimation(.easeOut(duration: 0.18)) {
                isExpanded = false
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .surfacePanel(cornerRadius: 20)
    }

    private var sortMenu: some View {
        Menu {
            ForEach(ReviewSortMode.allCases) { mode in
                Button {
                    sortMode = mode
                } label: {
                    if mode == sortMode {
                        Label(mode.title, systemImage: "checkmark")
                    } else {
                        Text(mode.title)
                    }
                }
            }
        } label: {
            Image(systemName: "arrow.up.arrow.down")
                .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(AppTheme.primary)
            .frame(width: 34, height: 34)
            .background(AppTheme.secondary.opacity(0.64), in: Capsule())
            .overlay(Capsule().stroke(AppTheme.border, lineWidth: 1))
            .accessibilityLabel("Sort reviews")
        }
        .buttonStyle(.plain)
    }

    private var ratingMenu: some View {
        Menu {
            Button {
                selectedRating = nil
            } label: {
                if selectedRating == nil {
                    Label("All ratings", systemImage: "checkmark")
                } else {
                    Text("All ratings")
                }
            }

            ForEach(availableRatings, id: \.self) { rating in
                Button {
                    selectedRating = rating
                } label: {
                    if selectedRating == rating {
                        Label("\(rating) stars", systemImage: "checkmark")
                    } else {
                        Text("\(rating) stars")
                    }
                }
            }
        } label: {
            Image(systemName: selectedRating == nil ? "star" : "star.fill")
                .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(AppTheme.primary)
            .frame(width: 34, height: 34)
            .background(AppTheme.secondary.opacity(0.64), in: Capsule())
            .overlay(Capsule().stroke(AppTheme.border, lineWidth: 1))
            .accessibilityLabel("Filter by rating")
        }
        .buttonStyle(.plain)
    }
}

private enum ReviewSortMode: String, CaseIterable, Identifiable {
    case defaultOrder
    case ratingHighToLow
    case ratingLowToHigh

    var id: String { rawValue }

    var title: String {
        switch self {
        case .defaultOrder: return "Default order"
        case .ratingHighToLow: return "Highest rated"
        case .ratingLowToHigh: return "Lowest rated"
        }
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
        .accessibilityLabel("\(rating) stars")
    }
}

struct AddToCartSheet: View {
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
                    Text("Confirm add to cart")
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
            .navigationTitle("Choose options")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            // 和聊天里的商品卡一样完整显示商品，不裁掉两端
            ProductRemoteImage(url: product.imageURL, cornerRadius: 16, placeholderIcon: "bag", contentMode: .fit)
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
            Text("Quantity")
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
                Text("Added to cart")
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
