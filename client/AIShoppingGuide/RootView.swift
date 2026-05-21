import SwiftUI

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

    var body: some View {
        ZStack(alignment: .bottom) {
            AppTheme.background.ignoresSafeArea()

            Group {
                switch selectedTab {
                case .guide:
                    GuideView(cartItems: $cartItems)
                case .cart:
                    CartView(items: $cartItems)
                case .preference:
                    PreferenceView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            LiquidTabBar(selectedTab: $selectedTab)
                .padding(.horizontal, 18)
                .padding(.bottom, 8)
        }
    }
}

struct LiquidTabBar: View {
    @Binding var selectedTab: AppTab

    var body: some View {
        HStack(spacing: 10) {
            ForEach(AppTab.allCases, id: \.self) { tab in
                Button {
                    selectedTab = tab
                } label: {
                    VStack(spacing: 4) {
                        Image(systemName: tab.icon)
                            .font(.system(size: 18, weight: .semibold))
                        Text(tab.rawValue)
                            .font(.caption2.weight(.medium))
                    }
                    .foregroundStyle(selectedTab == tab ? AppTheme.primary : AppTheme.textSecondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background {
                        if selectedTab == tab {
                            Capsule()
                                .fill(AppTheme.primary.opacity(0.12))
                        }
                    }
                }
                .buttonStyle(.plain)
            }
        }
        .padding(8)
        .glassPanel(cornerRadius: 30)
    }
}

#Preview {
    RootView()
}
