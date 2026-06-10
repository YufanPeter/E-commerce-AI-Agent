import SwiftUI
import UIKit

enum AppTab: String, CaseIterable {
    case guide = "导购"
    case cart = "购物车"
    case preference = "偏好"

    var icon: String {
        switch self {
        case .guide: return "bag"
        case .cart: return "cart"
        case .preference: return "person"
        }
    }
}

/// 底部导航栏：直接使用与 Preference 页肤质选择器相同的原生分段控件逻辑。
struct NativeTabBar: View {
    @Binding var selectedTab: AppTab

    var body: some View {
        SegmentedTabControl(selectedTab: $selectedTab)
            .frame(height: AppTheme.bottomTabBarHeight)
    }
}

/// 原生 UISegmentedControl 封装：纯图标、可放大、选中段为系统紫色。
struct SegmentedTabControl: UIViewRepresentable {
    @Binding var selectedTab: AppTab

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> UIView {
        let symbolConfig = UIImage.SymbolConfiguration(pointSize: 26, weight: .semibold)
        let control = UISegmentedControl(items: AppTab.allCases.map { tab in
            let img = UIImage(systemName: tab.icon, withConfiguration: symbolConfig)?
                .withRenderingMode(.alwaysTemplate)
            return img ?? UIImage()
        })
        control.selectedSegmentIndex = AppTab.allCases.firstIndex(of: selectedTab) ?? 0
        // 滑块保持白色，仅图标在选中时染紫
        control.selectedSegmentTintColor = UIColor.white
        control.apportionsSegmentWidthsByContent = false
        control.addTarget(context.coordinator,
                          action: #selector(Coordinator.valueChanged(_:)),
                          for: .valueChanged)

        // 放进容器，让分段控件填满容器，高度由 SwiftUI 的 frame 决定
        let container = UIView()
        container.backgroundColor = .clear
        control.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(control)
        NSLayoutConstraint.activate([
            control.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            control.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            control.topAnchor.constraint(equalTo: container.topAnchor),
            control.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        context.coordinator.control = control
        return container
    }

    func updateUIView(_ uiView: UIView, context: Context) {
        guard let control = context.coordinator.control else { return }
        let idx = AppTab.allCases.firstIndex(of: selectedTab) ?? 0
        if control.selectedSegmentIndex != idx {
            control.selectedSegmentIndex = idx
        }
        // 选中段图标染紫，其余为次要灰色
        for (i, tab) in AppTab.allCases.enumerated() {
            let tint: UIColor = (i == idx) ? UIColor(AppTheme.primary) : UIColor(AppTheme.textSecondary)
            let config = UIImage.SymbolConfiguration(pointSize: 26, weight: .semibold)
            let img = UIImage(systemName: tab.icon, withConfiguration: config)?
                .withTintColor(tint, renderingMode: .alwaysOriginal)
            control.setImage(img, forSegmentAt: i)
        }
    }

    final class Coordinator: NSObject {
        let parent: SegmentedTabControl
        weak var control: UISegmentedControl?
        init(_ parent: SegmentedTabControl) { self.parent = parent }

        @objc func valueChanged(_ sender: UISegmentedControl) {
            let idx = sender.selectedSegmentIndex
            guard AppTab.allCases.indices.contains(idx) else { return }
            parent.selectedTab = AppTab.allCases[idx]
        }
    }
}

struct RootView: View {
    @StateObject private var healthMonitor = BackendHealthMonitor()
    @State private var selectedTab: AppTab = .guide
    @State private var cartItems: [CartItem] = []

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

            BottomNavigationLayer(selectedTab: $selectedTab)
        }
        .safeAreaInset(edge: .top) {
            BackendStatusBanner(monitor: healthMonitor)
        }
        .task {
            healthMonitor.startMonitoring()
            await loadBackendCartOnLaunch()
        }
    }

    /// App 冷启动：从后端加载已有购物车（GET /cart），让购物车跨启动保留。
    /// 购物车在后端 SQLite 持久化，启动即拉取真实状态，避免"启动空车 + 首次加购回灌历史"导致的总价错乱。
    private func loadBackendCartOnLaunch() async {
        let service = RESTProductService()
        guard let snapshot = try? await service.fetchAgentCart() else { return }
        cartItems = snapshot.items.map(CartItem.init(payload:))
    }

}

private struct BottomNavigationLayer: View {
    @Binding var selectedTab: AppTab
    @State private var keyboardOffset: CGFloat = 0

    var body: some View {
        ZStack(alignment: .bottom) {
            // 底部模糊遮罩：卡片滑到导航栏区域时自然淡出，保证导航栏清晰可读
            Rectangle()
                .fill(AppTheme.surface.opacity(0.94))
                .frame(height: AppTheme.bottomTabBarHeight + 64)
                .mask(
                    LinearGradient(
                        colors: [.clear, .black.opacity(0.28), .black.opacity(0.7), .black],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                .allowsHitTesting(false)
                .ignoresSafeArea(edges: .bottom)
                .offset(y: keyboardOffset)

            NativeTabBar(selectedTab: $selectedTab)
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
        guard abs(keyboardOffset - offset) > 0.5 else { return }
        let duration = notification.userInfo?[UIResponder.keyboardAnimationDurationUserInfoKey] as? Double ?? 0.25

        withAnimation(.easeOut(duration: duration)) {
            keyboardOffset = offset
        }
    }
}

/// 后端不可达时显示的顶部引导横幅；可达时自动隐藏。
struct BackendStatusBanner: View {
    @ObservedObject var monitor: BackendHealthMonitor
    @State private var isRechecking = false

    var body: some View {
        Group {
            if monitor.status == .unreachable {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "wifi.exclamationmark")
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(AppTheme.error)

                    VStack(alignment: .leading, spacing: 2) {
                        Text("后端服务未连接")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                        Text("请在电脑上运行 ./scripts/dev.sh 启动后端（支持热重载）。")
                            .font(.system(size: 12))
                            .foregroundStyle(AppTheme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Spacer(minLength: 8)

                    Button {
                        guard !isRechecking else { return }
                        isRechecking = true
                        Task {
                            await monitor.check()
                            isRechecking = false
                        }
                    } label: {
                        Text(isRechecking ? "检测中…" : "重试连接")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(
                                Capsule(style: .continuous).fill(AppTheme.primary)
                            )
                    }
                    .disabled(isRechecking)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(AppTheme.surface)
                        .overlay(
                            RoundedRectangle(cornerRadius: 16, style: .continuous)
                                .stroke(AppTheme.error.opacity(0.35), lineWidth: 1)
                        )
                        .shadow(color: AppTheme.shadow.opacity(0.5), radius: 12, y: 4)
                )
                .padding(.horizontal, 16)
                .padding(.top, 6)
                .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.22), value: monitor.status)
    }
}

struct LiquidTabBar: View {
    @Binding var selectedTab: AppTab
    @State private var dragOffset: CGFloat = 0
    @State private var isDragging: Bool = false
    @State private var dragStartTab: AppTab?

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

            segmentDividers(metrics: metrics, pillX: pillX)

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
        selectionPill(width: metrics.itemWidth, x: pillX)
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
        let iconScale = 1 + response * (0.12 + movement * 0.05)
        let iconYOffset = -response * (1.0 + movement * 0.8)

        VStack(spacing: 3) {
            Image(systemName: tab.icon)
                .font(.system(size: 17, weight: .semibold))
                .symbolEffect(.bounce.down, value: selectedTab == tab)
                .scaleEffect(iconScale, anchor: .center)
                .offset(y: iconYOffset)
            Text(tab.rawValue)
                .font(.system(size: 11, weight: isHighlighted ? .semibold : .medium))
                .lineLimit(1)
                .minimumScaleFactor(0.78)
        }
        .foregroundStyle(isHighlighted ? AppTheme.primary : AppTheme.textSecondary.opacity(0.78))
        .frame(width: itemWidth)
        .frame(height: 48)
        .contentShape(Rectangle())
        .animation(.interactiveSpring(response: 0.24, dampingFraction: 0.82, blendDuration: 0.05), value: activation)
    }

    @ViewBuilder
    private func tabBarSurface(width: CGFloat) -> some View {
        RoundedRectangle(cornerRadius: 20, style: .continuous)
            .fill(AppTheme.tabBarSurface)
            .overlay(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .stroke(AppTheme.liquidStrokeStrong, lineWidth: 1)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .stroke(AppTheme.border, lineWidth: 0.6)
            )
            .shadow(color: AppTheme.shadow.opacity(0.8), radius: 18, y: 10)
            .frame(width: width, height: 64)
    }

    /// 分段选择器风格的竖向分隔线：靠近滑块的那几条淡出。
    @ViewBuilder
    private func segmentDividers(metrics: TabBarMetrics, pillX: CGFloat) -> some View {
        let dividerHeight: CGFloat = 22
        let yOffset = (64 - dividerHeight) / 2
        ForEach(1..<metrics.tabs.count, id: \.self) { i in
            let x = metrics.leadingX(for: i)
            let pillCenter = pillX + metrics.itemWidth / 2
            // 滑块越靠近该分隔线，线越淡
            let distance = abs(x - pillCenter)
            let fade = max(0, min(1, distance / (metrics.itemWidth * 0.6)))
            Rectangle()
                .fill(AppTheme.textSecondary.opacity(0.16 * fade))
                .frame(width: 1, height: dividerHeight)
                .offset(x: x - 0.5, y: yOffset)
                .allowsHitTesting(false)
        }
    }

    @ViewBuilder
    private func selectionPill(
        width: CGFloat,
        x: CGFloat
    ) -> some View {
        RoundedRectangle(cornerRadius: 15, style: .continuous)
            .fill(AppTheme.tabSelectionSurface)
            .overlay(
                RoundedRectangle(cornerRadius: 15, style: .continuous)
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
        RoundedRectangle(cornerRadius: 15, style: .continuous)
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
        let spacing: CGFloat = 0
        let hPad: CGFloat = 6

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

// #Preview {
//     RootView()
// }

#if DEBUG
#Preview("Root") {
    RootView()
}
#endif
