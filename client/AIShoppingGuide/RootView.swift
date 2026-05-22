import SwiftUI
import UIKit

enum AppTab: String, CaseIterable {
    case guide = "导购"
    case cart = "购物车"
    case preference = "Preference"

    var icon: String {
        switch self {
        case .guide: return "sparkles"
        case .cart: return "cart"
        case .preference: return "person"
        }
    }
}

struct RootView: View {
    @State private var selectedTab: AppTab = .guide
    @State private var cartItems: [Product] = Product.samples.prefix(2).map { $0 }
    @State private var keyboardOffset: CGFloat = 0

    var body: some View {
        ZStack(alignment: .bottom) {
            AppTheme.background.ignoresSafeArea()

            ZStack {
                GuideView(cartItems: $cartItems)
                    .opacity(selectedTab == .guide ? 1 : 0)
                    .allowsHitTesting(selectedTab == .guide)
                    .zIndex(selectedTab == .guide ? 1 : 0)

                CartView(items: $cartItems)
                    .opacity(selectedTab == .cart ? 1 : 0)
                    .allowsHitTesting(selectedTab == .cart)
                    .zIndex(selectedTab == .cart ? 1 : 0)

                PreferenceView()
                    .opacity(selectedTab == .preference ? 1 : 0)
                    .allowsHitTesting(selectedTab == .preference)
                    .zIndex(selectedTab == .preference ? 1 : 0)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .animation(.easeOut(duration: 0.16), value: selectedTab)

            LiquidTabBar(selectedTab: $selectedTab)
                .padding(.horizontal, 18)
                .padding(.bottom, AppTheme.bottomTabBarBottomPadding)
                .offset(y: keyboardOffset)
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillChangeFrameNotification)) { notification in
            updateKeyboardOffset(from: notification)
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { notification in
            updateKeyboardOffset(from: notification)
        }
    }

    private func updateKeyboardOffset(from notification: Notification) {
        guard let frame = notification.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect else {
            keyboardOffset = 0
            return
        }

        let screenHeight = UIApplication.shared.connectedScenes
            .compactMap { ($0 as? UIWindowScene)?.screen.bounds.height }
            .first ?? frame.maxY
        let offset = max(0, screenHeight - frame.minY)
        let duration = notification.userInfo?[UIResponder.keyboardAnimationDurationUserInfoKey] as? Double ?? 0.25

        withAnimation(.easeOut(duration: duration)) {
            keyboardOffset = offset
        }
    }
}

struct LiquidTabBar: View {
    @Binding var selectedTab: AppTab
    @State private var dragOffset: CGFloat = 0
    @State private var isDragging: Bool = false
    @State private var dragStartTab: AppTab?
    @Namespace private var glassNamespace

    var body: some View {
        GeometryReader { geo in
            let metrics = TabBarMetrics(width: geo.size.width, tabs: AppTab.allCases)
            let startTab = dragStartTab ?? selectedTab
            let startIndex = metrics.index(of: startTab)
            let basePillX = metrics.leadingX(for: startIndex)
            let pillX = basePillX + dragOffset
            let movement = min(1, abs(dragOffset) / max(metrics.step, 1))

            tabBarContent(
                metrics: metrics,
                basePillX: basePillX,
                pillX: pillX,
                movement: movement
            )
        }
        .frame(height: AppTheme.bottomTabBarHeight)
        .onChange(of: selectedTab) { _, _ in
            guard !isDragging else { return }
            dragStartTab = nil
            dragOffset = 0
        }
    }

    @ViewBuilder
    private func tabBarContent(
        metrics: TabBarMetrics,
        basePillX: CGFloat,
        pillX: CGFloat,
        movement: CGFloat
    ) -> some View {
        ZStack(alignment: .topLeading) {
            tabBarSurface(width: metrics.width)

            if isDragging {
                interactiveGlassLayer(
                    metrics: metrics,
                    pillX: pillX,
                    movement: movement
                )
            } else {
                selectionPill(
                    width: metrics.itemWidth,
                    x: pillX
                )
            }

            tabItems(metrics: metrics, pillX: pillX, movement: movement, isHighlighted: false)

            tabItems(metrics: metrics, pillX: pillX, movement: movement, isHighlighted: true)
                .mask(alignment: .topLeading) {
                    selectionMask(width: metrics.itemWidth, x: pillX)
                }
        }
        .contentShape(Rectangle())
        .highPriorityGesture(
            DragGesture(minimumDistance: 0)
                .onChanged { value in
                    if !isDragging {
                        dragStartTab = selectedTab
                        isDragging = true
                    }
                    let minX = metrics.leadingX(for: 0) - basePillX
                    let maxX = metrics.leadingX(for: metrics.tabs.count - 1) - basePillX
                    dragOffset = min(max(value.translation.width, minX), maxX)
                }
                .onEnded { value in
                    let movedFar = hypot(value.translation.width, value.translation.height) > 6
                    let target: AppTab

                    if movedFar {
                        let minX = metrics.leadingX(for: 0) - basePillX
                        let maxX = metrics.leadingX(for: metrics.tabs.count - 1) - basePillX
                        let projectedOffset = min(max(value.predictedEndTranslation.width, minX), maxX)
                        target = metrics.nearestTab(toLeadingX: basePillX + projectedOffset)
                    } else {
                        target = metrics.nearestTab(toLocationX: value.startLocation.x)
                    }

                    withAnimation(.interactiveSpring(response: 0.42, dampingFraction: 0.76, blendDuration: 0.12)) {
                        selectedTab = target
                        dragOffset = 0
                        isDragging = false
                        dragStartTab = nil
                    }
                }
        )
    }

    @ViewBuilder
    private func interactiveGlassLayer(
        metrics: TabBarMetrics,
        pillX: CGFloat,
        movement: CGFloat
    ) -> some View {
        if #available(iOS 26.0, *) {
            GlassEffectContainer(spacing: 22) {
                ZStack(alignment: .topLeading) {
                    Capsule(style: .continuous)
                        .fill(Color.white.opacity(0.001))
                        .glassEffect(
                            .regular
                                .interactive(true)
                                .tint(AppTheme.primary.opacity(0.08 + 0.12 * movement)),
                            in: Capsule(style: .continuous)
                        )
                        .glassEffectID("active-pill", in: glassNamespace)
                        .glassEffectTransition(.matchedGeometry)
                        .frame(width: metrics.itemWidth, height: 48)
                        .scaleEffect(1.14, anchor: .center)
                        .shadow(color: AppTheme.primary.opacity(0.16 + 0.10 * movement), radius: 14 + 8 * movement, y: 5)
                        .offset(x: pillX, y: 8)
                }
                .frame(width: metrics.width, height: 64, alignment: .topLeading)
            }
            .frame(width: metrics.width, height: 64, alignment: .topLeading)
            .clipped()
            .allowsHitTesting(false)
        } else {
            selectionPill(
                width: metrics.itemWidth,
                x: pillX
            )
        }
    }

    @ViewBuilder
    private func tabItems(
        metrics: TabBarMetrics,
        pillX: CGFloat,
        movement: CGFloat,
        isHighlighted: Bool
    ) -> some View {
        HStack(spacing: metrics.spacing) {
            ForEach(metrics.tabs, id: \.self) { tab in
                tabItem(
                    tab: tab,
                    itemWidth: metrics.itemWidth,
                    activation: activation(for: tab, pillX: pillX, metrics: metrics),
                    movement: movement,
                    isHighlighted: isHighlighted
                )
            }
        }
        .padding(.horizontal, metrics.hPad)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private func tabItem(
        tab: AppTab,
        itemWidth: CGFloat,
        activation: CGFloat,
        movement: CGFloat,
        isHighlighted: Bool
    ) -> some View {
        let response = activation * (0.62 + 0.38 * movement)
        let iconScale = 1 + response * (0.22 + movement * 0.08)
        let iconYOffset = -response * (1.8 + movement * 1.4)

        VStack(spacing: 0) {
            Image(systemName: tab.icon)
                .font(.system(size: 21, weight: .semibold))
                .symbolEffect(.bounce.down, value: selectedTab == tab)
                .scaleEffect(iconScale, anchor: .center)
                .offset(y: iconYOffset)
        }
        .foregroundStyle(isHighlighted ? AppTheme.primary : AppTheme.textSecondary.opacity(0.78))
        .frame(width: itemWidth)
        .frame(height: 48)
        .contentShape(Rectangle())
        .animation(.interactiveSpring(response: 0.24, dampingFraction: 0.82, blendDuration: 0.05), value: activation)
    }

    @ViewBuilder
    private func tabBarSurface(width: CGFloat) -> some View {
        Capsule(style: .continuous)
            .fill(AppTheme.tabBarSurface)
            .overlay(
                Capsule(style: .continuous)
                    .stroke(AppTheme.liquidStrokeStrong, lineWidth: 1)
            )
            .overlay(
                Capsule(style: .continuous)
                    .stroke(AppTheme.border, lineWidth: 0.6)
            )
            .shadow(color: AppTheme.shadow.opacity(0.8), radius: 18, y: 10)
            .frame(width: width, height: 64)
    }

    @ViewBuilder
    private func selectionPill(
        width: CGFloat,
        x: CGFloat
    ) -> some View {
        Capsule(style: .continuous)
            .fill(AppTheme.tabSelectionSurface)
            .overlay(
                Capsule(style: .continuous)
                    .stroke(AppTheme.liquidStrokeStrong, lineWidth: 1)
            )
            .shadow(color: AppTheme.shadow.opacity(0.55), radius: 7, y: 2)
            .allowsHitTesting(false)
            .frame(width: width, height: 48)
            .offset(x: x, y: 8)
            .animation(.interactiveSpring(response: 0.28, dampingFraction: 0.8, blendDuration: 0.08), value: selectedTab)
    }

    @ViewBuilder
    private func selectionMask(width: CGFloat, x: CGFloat) -> some View {
        Capsule(style: .continuous)
            .frame(width: width, height: 48)
            .scaleEffect(isDragging ? 1.12 : 1, anchor: .center)
            .offset(x: x, y: 8)
    }

    private func activation(for tab: AppTab, pillX: CGFloat, metrics: TabBarMetrics) -> CGFloat {
        let idx = metrics.index(of: tab)
        let tabCenter = metrics.centerX(for: idx)
        let pillCenter = pillX + metrics.itemWidth / 2
        let distance = abs(pillCenter - tabCenter)
        let t = max(0, min(1, 1 - distance / (metrics.step * 0.96)))
        return smoothstep(t)
    }

    private func smoothstep(_ t: CGFloat) -> CGFloat {
        t * t * (3 - 2 * t)
    }

    private struct TabBarMetrics {
        let width: CGFloat
        let tabs: [AppTab]
        let spacing: CGFloat = 10
        let hPad: CGFloat = 8

        var itemWidth: CGFloat {
            let count = CGFloat(max(tabs.count, 1))
            let totalSpacing = spacing * max(count - 1, 0)
            return max(0, (width - hPad * 2 - totalSpacing) / count)
        }

        var step: CGFloat {
            itemWidth + spacing
        }

        func index(of tab: AppTab) -> Int {
            tabs.firstIndex(of: tab) ?? 0
        }

        func leadingX(for index: Int) -> CGFloat {
            hPad + CGFloat(index) * step
        }

        func centerX(for index: Int) -> CGFloat {
            leadingX(for: index) + itemWidth / 2
        }

        func nearestTab(toLocationX locationX: CGFloat) -> AppTab {
            nearestTab(toLeadingX: locationX - itemWidth / 2)
        }

        func nearestTab(toLeadingX leadingX: CGFloat) -> AppTab {
            let rawIndex = (leadingX - hPad) / max(step, 1)
            let index = Int(rawIndex.rounded())
            let clampedIndex = max(0, min(tabs.count - 1, index))
            return tabs[clampedIndex]
        }
    }
}

#Preview {
    RootView()
}
