import SwiftUI
import UIKit

enum AppTab: String, CaseIterable {
    case guide = "Shop"
    case cart = "Cart"
    case preference = "Me"

    var icon: String {
        switch self {
        case .guide: return "bag"
        case .cart: return "cart"
        case .preference: return "person"
        }
    }
}

/// 常规电商底栏：图标加文字，选中项用实心图标和深色，购物车带数量角标。
struct ShopTabBar: View {
    @Binding var selectedTab: AppTab
    let cartCount: Int

    var body: some View {
        HStack(spacing: 0) {
            ForEach(AppTab.allCases, id: \.self) { tab in
                Button {
                    selectedTab = tab
                } label: {
                    item(tab)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(tab == .cart && cartCount > 0 ? "Cart, \(cartCount) items" : tab.rawValue)
                .accessibilityAddTraits(selectedTab == tab ? .isSelected : [])
            }
        }
        .frame(height: AppTheme.bottomTabBarHeight)
        .background(AppTheme.surface.ignoresSafeArea(edges: .bottom))
        .overlay(alignment: .top) {
            Rectangle().fill(AppTheme.border).frame(height: 0.5)
        }
    }

    private func item(_ tab: AppTab) -> some View {
        let isSelected = selectedTab == tab
        return VStack(spacing: 3) {
            Image(systemName: isSelected ? "\(tab.icon).fill" : tab.icon)
                .font(.system(size: 20, weight: .regular))
                .frame(height: 24)
                .overlay(alignment: .topTrailing) {
                    if tab == .cart && cartCount > 0 {
                        Text(cartCount > 99 ? "99+" : "\(cartCount)")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 4)
                            .frame(minWidth: 16, minHeight: 16)
                            .background(AppTheme.error, in: Capsule())
                            .offset(x: 10, y: -4)
                    }
                }
            Text(tab.rawValue)
                .font(.system(size: 10, weight: isSelected ? .semibold : .medium))
        }
        .foregroundStyle(isSelected ? AppTheme.textPrimary : AppTheme.textTertiary)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
    }
}

struct RootView: View {
    @StateObject private var healthMonitor = BackendHealthMonitor()
    @StateObject private var preferenceStore = PreferenceStore()
    @State private var selectedTab: AppTab = .guide
    @State private var cartItems: [CartItem] = []
    @State private var keyboardOffset: CGFloat = 0

    var body: some View {
        ZStack(alignment: .bottom) {
            AppTheme.background.ignoresSafeArea()

            ZStack {
                GuideView(cartItems: $cartItems, preferenceStore: preferenceStore, isActiveTab: selectedTab == .guide, onOpenCart: { selectedTab = .cart })
                    .opacity(selectedTab == .guide ? 1 : 0)
                    .allowsHitTesting(selectedTab == .guide)
                    .zIndex(selectedTab == .guide ? 1 : 0)

                CartView(items: $cartItems, onStartShopping: { selectedTab = .guide })
                    .opacity(selectedTab == .cart ? 1 : 0)
                    .allowsHitTesting(selectedTab == .cart)
                    .zIndex(selectedTab == .cart ? 1 : 0)

                PreferenceView(preferenceStore: preferenceStore, isVisible: selectedTab == .preference)
                    .opacity(selectedTab == .preference ? 1 : 0)
                    .allowsHitTesting(selectedTab == .preference)
                    .zIndex(selectedTab == .preference ? 1 : 0)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .animation(.easeOut(duration: 0.16), value: selectedTab)

            ShopTabBar(selectedTab: $selectedTab, cartCount: cartItems.reduce(0) { $0 + $1.quantity })
                .offset(y: keyboardOffset)
        }
        .safeAreaInset(edge: .top) {
            BackendStatusBanner(monitor: healthMonitor)
        }
        .task {
            healthMonitor.startMonitoring()
            await loadBackendCartOnLaunch()
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillChangeFrameNotification)) { notification in
            updateKeyboardOffset(from: notification)
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { notification in
            updateKeyboardOffset(from: notification)
        }
    }

    /// On a cold launch, load the persisted cart from the backend with `GET /cart`.
    /// Fetching the SQLite-backed state immediately prevents totals from being corrupted by a stale empty cart.
    private func loadBackendCartOnLaunch() async {
        let service = RESTProductService()
        guard let snapshot = try? await service.fetchAgentCart() else { return }
        cartItems = snapshot.items.map(CartItem.init(payload:))
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

/// Guidance banner shown when the backend is unreachable and hidden automatically after recovery.
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
                        Text("Backend is disconnected")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                        Text("Start the Amazon backend on your computer, then try again.")
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
                        Text(isRechecking ? "Checking…" : "Reconnect")
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

// #Preview {
//     RootView()
// }

#if DEBUG
#Preview("Root") {
    RootView()
}
#endif
