import SwiftUI

struct CartView: View {
    @Binding var items: [CartItem]

    private var itemCount: Int {
        items.reduce(0) { $0 + $1.quantity }
    }

    private var total: Double {
        items.reduce(0) { partialResult, item in
            partialResult + priceValue(for: item.product) * Double(item.quantity)
        }
    }

    private var totalText: String {
        if total.rounded(.towardZero) == total {
            return "¥\(Int(total))"
        }
        return "¥\(String(format: "%.2f", total))"
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
                            ForEach($items) { $item in
                                CartRow(item: $item) {
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
                            Text(totalText)
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
            .navigationTitle(itemCount > 0 ? "购物车(\(itemCount))" : "购物车")
        }
    }

    private func priceValue(for product: Product) -> Double {
        let cleaned = product.price
            .replacingOccurrences(of: "¥", with: "")
            .replacingOccurrences(of: ",", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return Double(cleaned) ?? 0
    }
}

struct CartRow: View {
    @Binding var item: CartItem
    let onRemove: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(AppTheme.softPurple)
                .frame(width: 78, height: 78)
                .overlay(Image(systemName: "shippingbox").foregroundStyle(AppTheme.primary))

            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 10) {
                    Text(item.product.title)
                        .font(.headline)
                        .foregroundStyle(AppTheme.textPrimary)
                        .lineLimit(2)

                    Spacer(minLength: 6)

                    Button(action: onRemove) {
                        Image(systemName: "trash")
                            .foregroundStyle(AppTheme.textSecondary)
                            .frame(width: 32, height: 32)
                    }
                    .buttonStyle(.plain)
                }

                Text(item.product.reason)
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
                    .lineLimit(2)

                if !item.specificationSummary.isEmpty {
                    Text(item.specificationSummary)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(AppTheme.textSecondary)
                        .lineLimit(1)
                }

                HStack(alignment: .center, spacing: 10) {
                    Text(item.product.price)
                        .font(.headline)
                        .foregroundStyle(AppTheme.error)

                    Spacer(minLength: 4)

                    QuantityControl(quantity: $item.quantity)
                }
            }
        }
        .padding(12)
        .floatingLiquidPanel(cornerRadius: 22)
    }
}

struct QuantityControl: View {
    @Binding var quantity: Int
    @State private var isEditing = false
    @State private var draftQuantity = ""
    @FocusState private var isFocused: Bool

    private let minQuantity = 1
    private let maxQuantity = 99

    var body: some View {
        HStack(spacing: 0) {
            quantityButton(systemName: "minus") {
                quantity = max(minQuantity, quantity - 1)
            }
            .disabled(quantity <= minQuantity)
            .opacity(quantity <= minQuantity ? 0.35 : 1)

            Divider()
                .frame(height: 20)

            Group {
                if isEditing {
                    TextField("", text: $draftQuantity)
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.center)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(AppTheme.textPrimary)
                        .focused($isFocused)
                        .onChange(of: draftQuantity) { _, newValue in
                            let filtered = newValue.filter(\.isNumber)
                            if filtered != newValue {
                                draftQuantity = filtered
                            }
                        }
                        .onChange(of: isFocused) { _, focused in
                            if !focused {
                                commitDraft()
                            }
                        }
                        .toolbar {
                            ToolbarItemGroup(placement: .keyboard) {
                                Spacer()
                                Button("完成") {
                                    commitDraft()
                                    isFocused = false
                                }
                            }
                        }
                } else {
                    Button {
                        beginEditing()
                    } label: {
                        Text("\(quantity)")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(AppTheme.textPrimary)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("修改数量，当前 \(quantity)")
                }
            }
            .frame(width: 42, height: 32)

            Divider()
                .frame(height: 20)

            quantityButton(systemName: "plus") {
                quantity = min(maxQuantity, quantity + 1)
            }
            .disabled(quantity >= maxQuantity)
            .opacity(quantity >= maxQuantity ? 0.35 : 1)
        }
        .frame(width: 116, height: 34)
        .background(AppTheme.secondary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(AppTheme.border, lineWidth: 0.6)
        )
    }

    private func quantityButton(systemName: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(AppTheme.textPrimary)
                .frame(width: 36, height: 32)
        }
        .buttonStyle(.plain)
    }

    private func beginEditing() {
        draftQuantity = "\(quantity)"
        withAnimation(.easeOut(duration: 0.12)) {
            isEditing = true
        }
        DispatchQueue.main.async {
            isFocused = true
        }
    }

    private func commitDraft() {
        let parsed = Int(draftQuantity) ?? quantity
        quantity = min(max(parsed, minQuantity), maxQuantity)
        draftQuantity = "\(quantity)"
        isEditing = false
    }
}
