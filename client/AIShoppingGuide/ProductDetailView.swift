import SwiftUI

struct ProductDetailView: View {
    let product: Product
    let onAddToCart: () -> Void
    @State private var added = false

    var body: some View {
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
                .background(.white, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .stroke(AppTheme.border, lineWidth: 1)
                )

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
                    onAddToCart()
                    added = true
                } label: {
                    Text(added ? "已加入购物车" : "加入购物车")
                        .font(.headline)
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 15)
                        .background(AppTheme.primary, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
                .padding(.top, 8)
            }
            .padding(20)
            .padding(.bottom, 92)
        }
        .background(AppTheme.background)
        .navigationTitle("商品详情")
        .navigationBarTitleDisplayMode(.inline)
    }
}
