import AVFoundation
import SwiftUI

struct ProductDetailView: View {
    let product: Product
    /// AI 在聊天里写的推荐理由；从购物车等地方打开时没有。
    let recommendation: String?
    let onAddToCart: (Product, [String: String], Int) -> Void

    @State private var selectedOptions: [String: String]
    @State private var quantity = 1
    @State private var showSuccessToast = false
    @State private var toastID = UUID()
    @State private var successQuantity = 1
    @State private var successSummary = ""
    @State private var isListingExpanded = false
    @State private var isTitleExpanded = false

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
        recommendation: String? = nil,
        onAddToCart: @escaping (Product, [String: String], Int) -> Void
    ) {
        self.product = product
        self.recommendation = recommendation
        self.onAddToCart = onAddToCart
        _selectedOptions = State(initialValue: product.defaultSpecificationSelection)
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    productHeroImage

                    VStack(alignment: .leading, spacing: 0) {
                        productInfo
                            .padding(.vertical, 16)

                        if hasChoices {
                            sectionDivider
                            optionPicker
                                .padding(.vertical, 16)
                        }

                        // 这里只放 AI 的一两句推荐理由；商家原文在下面，默认折叠
                        if let recommendation, !recommendation.isEmpty {
                            whyThisOne(recommendation)
                                .padding(.bottom, 16)
                        }

                        if !listingDescription.isEmpty {
                            sectionDivider
                            listingDescriptionBlock
                                .padding(.vertical, 16)
                        }

                        if !(product.faqs ?? []).isEmpty {
                            sectionDivider
                            ProductFAQSection(faqs: product.faqs ?? [])
                                .padding(.vertical, 16)
                        }

                        if !product.reviews.isEmpty {
                            sectionDivider
                            ProductReviewsSection(
                                reviews: product.reviews,
                                rating: product.rating,
                                ratingCount: product.ratingCount
                            )
                            .padding(.vertical, 16)
                        }
                    }
                    .padding(.horizontal, 20)
                }
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
                purchaseBar
            }
        }
        .background(AppTheme.background)
        .navigationTitle("")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                ListenHighlightsButton(product: product)
            }
        }
        .toolbarBackground(AppTheme.background, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
    }

    private var sectionDivider: some View {
        Rectangle().fill(AppTheme.border).frame(height: 0.5)
    }

    /// 底部购买栏：左边是当前规格的价格，右边加购。规格已在页面上选好，点了直接加。
    private var purchaseBar: some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 1) {
                Text("Price")
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                Text(product.priceDisplay(for: selectedOptions))
                    .font(.headline)
                    .foregroundStyle(AppTheme.textPrimary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            Button {
                confirmAddToCart()
            } label: {
                Text("Add to cart")
                    .font(.headline)
                    .foregroundStyle(AppTheme.background)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            .buttonStyle(.tactile)
        }
        .padding(.horizontal, 20)
        .padding(.bottom, stickyButtonBottomPadding)
    }

    /// 加购按钮和标签栏下面的实底，顶部一小段渐隐。半透明遮罩挡不住，正文会从按钮和标签栏之间透出来。
    private var detailBottomScrim: some View {
        VStack(spacing: 0) {
            LinearGradient(
                colors: [AppTheme.background.opacity(0), AppTheme.background],
                startPoint: .top,
                endPoint: .bottom
            )
            .frame(height: 22)
            Color.clear
                .frame(height: stickyButtonBottomPadding + 60)
                .background(AppTheme.background.ignoresSafeArea(edges: .bottom))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
        .allowsHitTesting(false)
    }

    /// 商品图铺满宽度，白底，不加卡片和阴影
    private var productHeroImage: some View {
        ProductRemoteImage(url: product.imageURL, cornerRadius: 0, placeholderIcon: "bag", contentMode: .fit)
            .padding(24)
            .frame(maxWidth: .infinity)
            .frame(height: 340)
            .background(Color.white)
            .overlay(alignment: .bottom) { sectionDivider }
    }

    private var productInfo: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(product.priceDisplay(for: selectedOptions))
                .font(.system(size: 26, weight: .semibold))
                .foregroundStyle(AppTheme.textPrimary)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
            Text(product.title)
                .font(.body)
                .foregroundStyle(AppTheme.textPrimary)
                .lineSpacing(2)
                .lineLimit(isTitleExpanded ? nil : 3)
                .fixedSize(horizontal: false, vertical: true)
                .onTapGesture { withAnimation(.easeOut(duration: 0.18)) { isTitleExpanded.toggle() } }
            if let rating = product.rating {
                HStack(spacing: 6) {
                    StarRow(rating: rating)
                    Text(String(format: "%.1f", rating))
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(AppTheme.textPrimary)
                    if let count = product.ratingCount, count > 0 {
                        Text("· \(count.formatted()) ratings")
                            .font(.footnote)
                            .foregroundStyle(AppTheme.textSecondary)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var hasChoices: Bool {
        product.specifications.contains { $0.options.count > 1 }
    }

    /// 规格直接在页面上选，不再先弹一层选择页
    private var optionPicker: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(product.specifications.filter { $0.options.count > 1 }) { specification in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 4) {
                        Text("\(specification.name):")
                            .foregroundStyle(AppTheme.textSecondary)
                        Text(selectedOptions[specification.name] ?? "")
                            .foregroundStyle(AppTheme.textPrimary)
                    }
                    .font(.subheadline)
                    FlowLayout(spacing: 8) {
                        ForEach(specification.options, id: \.self) { option in
                            let isSelected = selectedOptions[specification.name] == option
                            Button {
                                selectedOptions[specification.name] = option
                            } label: {
                                Text(option)
                                    .font(.subheadline)
                                    .foregroundStyle(AppTheme.textPrimary)
                                    .padding(.horizontal, 14)
                                    .padding(.vertical, 8)
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                                            .stroke(isSelected ? AppTheme.textPrimary : AppTheme.border, lineWidth: isSelected ? 1.5 : 1)
                                    )
                            }
                            .buttonStyle(.tactile)
                        }
                    }
                }
            }
        }
    }

    private func whyThisOne(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            AIByline(title: "Why this one")
            Text(text)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.secondary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    /// 商家原文。【卖点】各起一行，方便扫读。
    private var listingDescription: String {
        product.reason
            .replacingOccurrences(of: "【", with: "\n【")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var listingDescriptionBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("About this item")
                .font(.headline)
                .foregroundStyle(AppTheme.textPrimary)
            Text(listingDescription)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textSecondary)
                .lineSpacing(4)
                .lineLimit(isListingExpanded ? nil : 4)
                .fixedSize(horizontal: false, vertical: true)
            Button(isListingExpanded ? "Show less" : "Show more") {
                withAnimation(.easeOut(duration: 0.2)) {
                    isListingExpanded.toggle()
                }
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(AppTheme.textPrimary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var selectedSpecificationSummary: String {
        selectedOptions
            .sorted { $0.key < $1.key }
            .map { "\($0.key): \($0.value)" }
            .joined(separator: "  ")
    }

    private func confirmAddToCart() {
        onAddToCart(product, selectedOptions, quantity)
        successQuantity = quantity
        successSummary = selectedSpecificationSummary
        toastID = UUID()
        let currentToastID = toastID

        withAnimation(.spring(response: 0.28, dampingFraction: 0.82)) {
            showSuccessToast = true
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            guard currentToastID == toastID else { return }
            withAnimation(.easeOut(duration: 0.18)) {
                showSuccessToast = false
            }
        }
    }
}

/// 五颗星，按评分填满、半颗或空心
struct StarRow: View {
    let rating: Double
    var size: CGFloat = 11

    var body: some View {
        HStack(spacing: 1) {
            ForEach(1...5, id: \.self) { index in
                let value = rating - Double(index - 1)
                Image(systemName: value >= 0.75 ? "star.fill" : (value >= 0.25 ? "star.leadinghalf.filled" : "star"))
                    .font(.system(size: size))
                    .foregroundStyle(AppTheme.star)
            }
        }
        .accessibilityLabel(String(format: "%.1f out of 5 stars", rating))
    }
}

/// 导航栏右上角的"听亮点"小按钮
private struct ListenHighlightsButton: View {
    let product: Product

    @StateObject private var speaker = HighlightsSpeaker()
    private let productService = RESTProductService()

    var body: some View {
        Button {
            Task { await toggle() }
        } label: {
            Group {
                if speaker.state == .loading {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: speaker.state == .speaking ? "stop.circle" : "speaker.wave.2")
                }
            }
            .foregroundStyle(AppTheme.textPrimary)
        }
        .disabled(speaker.state == .loading)
        .accessibilityLabel(speaker.state == .speaking ? "Stop audio" : "Listen to highlights")
        .onDisappear { speaker.stop() }
    }

    /// 优先念模型写的短卖点，取不到就念商品介绍。
    private func toggle() async {
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

/// 商品问答：问题一行一条，点开看回答
private struct ProductFAQSection: View {
    let faqs: [ProductFAQ]
    @State private var openFAQ: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Questions and answers")
                .font(.headline)
                .foregroundStyle(AppTheme.textPrimary)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(faqs.enumerated()), id: \.element.id) { index, faq in
                    if index > 0 { Divider() }
                    faqRow(faq)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
    let rating: Double?
    let ratingCount: Int?

    @State private var isExpanded = false
    @State private var selectedRating: Int?
    @State private var sortMode: ReviewSortMode = .defaultOrder

    private let collapsedCount = 2

    private var visibleReviews: [ProductReview] {
        isExpanded ? filteredAndSortedReviews : Array(filteredAndSortedReviews.prefix(collapsedCount))
    }

    private var hiddenReviewCount: Int {
        max(0, filteredAndSortedReviews.count - collapsedCount)
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

    /// 商品整体评分；后端没给就用已加载评论的平均分
    private var overallRating: Double {
        if let rating { return rating }
        guard !reviews.isEmpty else { return 0 }
        return Double(reviews.map(\.rating).reduce(0, +)) / Double(reviews.count)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Customer reviews")
                .font(.headline)
                .foregroundStyle(AppTheme.textPrimary)

            summary

            HStack(spacing: 8) {
                ratingMenu
                sortMenu
                Spacer(minLength: 0)
            }

            if visibleReviews.isEmpty {
                Text("No reviews match this filter.")
                    .font(.subheadline)
                    .foregroundStyle(AppTheme.textSecondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 6)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(visibleReviews) { review in
                        if review.id != visibleReviews.first?.id {
                            Divider().overlay(AppTheme.border)
                        }
                        ProductReviewRow(review: review)
                            .padding(.vertical, 12)
                    }
                }
            }

            if hiddenReviewCount > 0 {
                Button {
                    withAnimation(.easeOut(duration: 0.18)) {
                        isExpanded.toggle()
                    }
                } label: {
                    Text(isExpanded ? "Show fewer reviews" : "See \(hiddenReviewCount) more reviews")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(AppTheme.textPrimary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                        .overlay(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .stroke(AppTheme.border, lineWidth: 1)
                        )
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
    }

    private var summary: some View {
        HStack(alignment: .center, spacing: 12) {
            Text(String(format: "%.1f", overallRating))
                .font(.system(size: 34, weight: .semibold))
                .foregroundStyle(AppTheme.textPrimary)
            VStack(alignment: .leading, spacing: 3) {
                StarRow(rating: overallRating, size: 13)
                Text(ratingCountText)
                    .font(.footnote)
                    .foregroundStyle(AppTheme.textSecondary)
            }
            Spacer(minLength: 0)
        }
    }

    private var ratingCountText: String {
        if let ratingCount, ratingCount > 0 {
            return "\(ratingCount.formatted()) global ratings"
        }
        return "\(reviews.count) reviews"
    }

    private func filterChip(_ title: String, systemImage: String) -> some View {
        HStack(spacing: 4) {
            Text(title)
            Image(systemName: systemImage)
                .font(.caption2.weight(.semibold))
        }
        .font(.footnote)
        .foregroundStyle(AppTheme.textPrimary)
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(AppTheme.secondary, in: Capsule())
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
            filterChip(sortMode == .defaultOrder ? "Sort" : sortMode.title, systemImage: "chevron.down")
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
            filterChip(selectedRating.map { "\($0) stars" } ?? "All ratings", systemImage: "chevron.down")
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
            HStack(alignment: .center, spacing: 8) {
                StarRow(rating: Double(review.rating), size: 11)
                Text(review.nickname)
                    .font(.footnote)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(1)
            }

            Text(review.content)
                .font(.subheadline)
                .foregroundStyle(AppTheme.textPrimary)
                .lineSpacing(3)
                .lineLimit(6)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
                    Text("Add to cart")
                        .font(.headline)
                        .foregroundStyle(AppTheme.background)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 14)
                        .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
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
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(AppTheme.textPrimary)
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
        .surfacePanel(cornerRadius: 12)
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
        .surfacePanel(cornerRadius: 12)
    }

    private var quantityControl: some View {
        HStack(alignment: .center) {
            Text("Quantity")
                .font(.headline)
            Spacer()
            QuantityControl(quantity: $quantity)
        }
        .padding(16)
        .surfacePanel(cornerRadius: 12)
    }

    private var selectedSpecificationSummary: String {
        selectedOptions
            .sorted { $0.key < $1.key }
            .map { "\($0.key): \($0.value)" }
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
                .foregroundStyle(AppTheme.textPrimary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .padding(.horizontal, 8)
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(isSelected ? AppTheme.textPrimary : AppTheme.border, lineWidth: isSelected ? 1.5 : 1)
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
        .surfacePanel(cornerRadius: 12)
    }

    private var detailText: String {
        let quantityText = "x\(quantity)"
        guard !summary.isEmpty else { return quantityText }
        return "\(summary)  \(quantityText)"
    }
}
