import SwiftUI
import UIKit

struct AppTheme {
    // White and warm beige surfaces with a restrained terracotta accent.
    static let primary = Color.dynamic(lightHex: "CE8763", darkHex: "EBA886")      // 浅陶土/暖赭强调色
    static let secondary = Color.dynamic(lightHex: "F5EFE7", darkHex: "2A231D")     // 浅米填充
    static let softBlue = Color.dynamic(lightHex: "F6F0E8", darkHex: "211C17")      // 用户气泡（浅米）
    static let softPurple = Color.dynamic(lightHex: "F4EEE5", darkHex: "241E18")    // 芯片/规格填充（浅米）
    static let background = Color.dynamic(lightHex: "FBFAF7", darkHex: "13110F")     // 近白·极淡米底
    static let surface = Color.dynamic(lightHex: "FFFFFF", darkHex: "1E1A16")        // 卡片表面（纯白）
    static let textPrimary = Color.dynamic(lightHex: "2B2520", darkHex: "F2ECE4")    // 暖近黑
    static let textSecondary = Color.dynamic(lightHex: "8A7E72", darkHex: "A89C8E")  // 暖灰
    static let success = Color.dynamic(lightHex: "3F9F6B", darkHex: "5BD08C")
    static let error = Color.dynamic(lightHex: "D2543E", darkHex: "F0846E")
    static let border = Color.dynamic(
        light: UIColor(hex: "2B2520").withAlphaComponent(0.08),
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
        light: UIColor.white.withAlphaComponent(0.96),
        dark: UIColor(hex: "1D1916").withAlphaComponent(0.96)
    )
    static let tabSelectionSurface = Color.dynamic(
        light: UIColor(hex: "F2E8DE"),
        dark: UIColor(hex: "2A241E")
    )
    static let shadow = Color.dynamic(
        light: UIColor(hex: "2B1E12").withAlphaComponent(0.05),
        dark: UIColor.black.withAlphaComponent(0.24)
    )
    static let accentGlow = Color.dynamic(
        light: UIColor(hex: "C2613C").withAlphaComponent(0.03),
        dark: UIColor(hex: "E89A72").withAlphaComponent(0.04)
    )

    static let bottomTabBarHeight: CGFloat = 72
    static let bottomTabBarBottomPadding: CGFloat = -14
    static let goldenRatioMinor: CGFloat = 0.382
    static let guideComposerTabGap: CGFloat = 16
    static let guideComposerBottomPadding = bottomTabBarHeight + bottomTabBarBottomPadding + guideComposerTabGap
    static let cartCheckoutTabGap: CGFloat = 22
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
                    .stroke(AppTheme.border, lineWidth: 1)
            )
            .shadow(color: AppTheme.shadow.opacity(0.45), radius: 5, y: 2)
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

    func surfacePanel(cornerRadius: CGFloat = 20) -> some View {
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
