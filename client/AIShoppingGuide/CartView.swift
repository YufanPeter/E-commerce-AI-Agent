import SwiftUI

struct CartView: View {
    @Binding var items: [Product]

    private var total: Int {
        items.compactMap { Int($0.price.replacingOccurrences(of: "¥", with: "")) }.reduce(0, +)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if items.isEmpty {
                    ContentUnavailableView("购物车为空", systemImage: "cart", description: Text("从导购推荐里加入商品后会显示在这里。"))
                        .padding(.bottom, 90)
                } else {
                    ScrollView(showsIndicators: false) {
                        VStack(spacing: 12) {
                            ForEach(items) { item in
                                CartRow(product: item) {
                                    items.removeAll { $0.id == item.id }
                                }
                            }
                        }
                        .padding(18)
                        .padding(.bottom, AppTheme.cartCheckoutBottomPadding + 92)
                    }
                }
            }
            .safeAreaInset(edge: .bottom) {
                if !items.isEmpty {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("合计")
                                .font(.caption)
                                .foregroundStyle(AppTheme.textSecondary)
                            Text("¥\(total)")
                                .font(.title3.bold())
                                .foregroundStyle(AppTheme.primary)
                        }
                        Spacer()
                        Button("去结算") {}
                            .font(.headline)
                            .foregroundStyle(.white)
                            .padding(.horizontal, 26)
                            .padding(.vertical, 13)
                            .background(AppTheme.primary, in: Capsule())
                    }
                    .padding(.horizontal, 20)
                    .padding(.top, 12)
                    .padding(.bottom, 12)
                    .floatingLiquidPanel(cornerRadius: 26)
                    .padding(.horizontal, 16)
                    .padding(.bottom, AppTheme.cartCheckoutBottomPadding)
                }
            }
            .background(AppTheme.background)
            .navigationTitle("购物车")
        }
    }
}

struct CartRow: View {
    let product: Product
    let onRemove: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(AppTheme.softPurple)
                .frame(width: 78, height: 78)
                .overlay(Image(systemName: "shippingbox").foregroundStyle(AppTheme.primary))

            VStack(alignment: .leading, spacing: 6) {
                Text(product.title)
                    .font(.headline)
                    .lineLimit(2)
                Text(product.reason)
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(2)
                Text(product.price)
                    .font(.headline)
                    .foregroundStyle(AppTheme.error)
            }
            Spacer()
            Button(action: onRemove) {
                Image(systemName: "trash")
                    .foregroundStyle(AppTheme.textSecondary)
            }
        }
        .padding(12)
        .floatingLiquidPanel(cornerRadius: 22)
    }
}
