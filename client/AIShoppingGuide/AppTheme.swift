import SwiftUI
import UIKit

struct AppTheme {
    // 电商常见的中性底色：白底、浅灰填充、近黑文字。主操作用墨色，陶土色只留给 AI 的标记。
    static let primary = Color.dynamic(lightHex: "1D1D1F", darkHex: "F5F5F7")      // 主操作（墨色）
    static let brand = Color.dynamic(lightHex: "C8693F", darkHex: "E89A72")        // AI 标记专用
    static let secondary = Color.dynamic(lightHex: "F4F4F5", darkHex: "2C2C2E")     // 浅灰填充
    static let softBlue = Color.dynamic(lightHex: "F2F2F4", darkHex: "2C2C2E")      // 用户气泡
    static let softPurple = Color.dynamic(lightHex: "F4F4F5", darkHex: "2C2C2E")    // 标签/规格填充
    static let background = Color.dynamic(lightHex: "FFFFFF", darkHex: "000000")
    static let surface = Color.dynamic(lightHex: "FFFFFF", darkHex: "1C1C1E")
    static let textPrimary = Color.dynamic(lightHex: "1D1D1F", darkHex: "F5F5F7")
    static let textSecondary = Color.dynamic(lightHex: "6E6E73", darkHex: "A1A1A6")
    static let textTertiary = Color.dynamic(lightHex: "A1A1A6", darkHex: "6E6E73")
    static let success = Color.dynamic(lightHex: "1F8A4C", darkHex: "4CD47F")
    static let error = Color.dynamic(lightHex: "D2382B", darkHex: "FF6B5E")
    static let star = Color.dynamic(lightHex: "E8A317", darkHex: "F5B73B")
    static let border = Color.dynamic(
        light: UIColor.black.withAlphaComponent(0.09),
        dark: UIColor.white.withAlphaComponent(0.11)
    )
    static let liquidOverlay = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.10),
        dark: UIColor.white.withAlphaComponent(0.03)
    )
    static let liquidStrokeStrong = Color.dynamic(
        light: UIColor(hex: "2B2520").withAlphaComponent(0.10),
        dark: UIColor.white.withAlphaComponent(0.12)
    )
    static let liquidStrokeSoft = Color.dynamic(
        light: UIColor(hex: "2B2520").withAlphaComponent(0.05),
        dark: UIColor.white.withAlphaComponent(0.05)
    )
    static let tabBarSurface = Color.dynamic(
        light: UIColor.white,
        dark: UIColor(hex: "1C1C1E")
    )
    static let tabSelectionSurface = Color.dynamic(
        light: UIColor(hex: "F4F4F5"),
        dark: UIColor(hex: "2C2C2E")
    )
    static let shadow = Color.dynamic(
        light: UIColor(hex: "2B1E12").withAlphaComponent(0.05),
        dark: UIColor.black.withAlphaComponent(0.24)
    )
    static let accentGlow = Color.dynamic(
        light: UIColor(hex: "C2613C").withAlphaComponent(0.03),
        dark: UIColor(hex: "E89A72").withAlphaComponent(0.04)
    )

    static let bottomTabBarHeight: CGFloat = 52
    static let bottomTabBarBottomPadding: CGFloat = 0
    static let goldenRatioMinor: CGFloat = 0.382
    static let guideComposerTabGap: CGFloat = 10
    static let guideComposerBottomPadding = bottomTabBarHeight + bottomTabBarBottomPadding + guideComposerTabGap
    static let cartCheckoutTabGap: CGFloat = 0
    static let cartCheckoutBottomPadding = bottomTabBarHeight + bottomTabBarBottomPadding + cartCheckoutTabGap
}

extension Color {
    init(hex: String) {
        self.init(UIColor(hex: hex))
    }

    static func dynamic(lightHex: String, darkHex: String) -> Color {
        dynamic(light: UIColor(hex: lightHex), dark: UIColor(hex: darkHex))
    }

    static func dynamic(light: UIColor, dark: UIColor) -> Color {
        Color(UIColor { traits in
            traits.userInterfaceStyle == .dark ? dark : light
        })
    }
}

private extension UIColor {
    convenience init(hex: String) {
        let value = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        let scanner = Scanner(string: value)
        var rgb: UInt64 = 0
        scanner.scanHexInt64(&rgb)

        let red = CGFloat((rgb >> 16) & 0xFF) / 255
        let green = CGFloat((rgb >> 8) & 0xFF) / 255
        let blue = CGFloat(rgb & 0xFF) / 255
        self.init(red: red, green: green, blue: blue, alpha: 1)
    }
}

struct GlassPanelModifier: ViewModifier {
    let cornerRadius: CGFloat

    func body(content: Content) -> some View {
        content
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(AppTheme.liquidStrokeStrong, lineWidth: 1)
            )
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(AppTheme.border, lineWidth: 0.5)
            )
    }
}

struct SurfacePanelModifier: ViewModifier {
    let cornerRadius: CGFloat

    func body(content: Content) -> some View {
        content
            .background(AppTheme.surface, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(AppTheme.border, lineWidth: 0.5)
            )
    }
}

struct TactileButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .opacity(configuration.isPressed ? 0.82 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

extension View {
    func glassPanel(cornerRadius: CGFloat = 24) -> some View {
        modifier(GlassPanelModifier(cornerRadius: cornerRadius))
    }

    /// 卡片只用细线，不加阴影；圆角统一偏小，避免满屏圆胖卡片
    func surfacePanel(cornerRadius: CGFloat = 12) -> some View {
        modifier(SurfacePanelModifier(cornerRadius: cornerRadius))
    }

    func floatingLiquidPanel(cornerRadius: CGFloat = 28) -> some View {
        background(AppTheme.surface, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(AppTheme.border, lineWidth: 1)
            )
            .shadow(color: AppTheme.shadow.opacity(0.4), radius: 6, y: 2)
    }
}

extension ButtonStyle where Self == TactileButtonStyle {
    static var tactile: TactileButtonStyle { TactileButtonStyle() }
}
