import SwiftUI
import UIKit

struct AppTheme {
    static let primary = Color.dynamic(lightHex: "6C5CE7", darkHex: "9A8CFF")
    static let secondary = Color.dynamic(lightHex: "F2F3FF", darkHex: "232235")
    static let softBlue = Color.dynamic(lightHex: "F4F0FF", darkHex: "202231")
    static let softPurple = Color.dynamic(lightHex: "F2F3FF", darkHex: "26233A")
    static let background = Color.dynamic(lightHex: "F7F8FA", darkHex: "0F1014")
    static let surface = Color.dynamic(lightHex: "FFFFFF", darkHex: "1A1B21")
    static let textPrimary = Color.dynamic(lightHex: "1D1E20", darkHex: "F2F3F7")
    static let textSecondary = Color.dynamic(lightHex: "637280", darkHex: "A4ADBA")
    static let success = Color.dynamic(lightHex: "22C55E", darkHex: "4ADE80")
    static let error = Color.dynamic(lightHex: "EF4444", darkHex: "FF6B6B")
    static let border = Color.dynamic(
        light: UIColor.black.withAlphaComponent(0.08),
        dark: UIColor.white.withAlphaComponent(0.12)
    )
    static let liquidOverlay = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.22),
        dark: UIColor.white.withAlphaComponent(0.06)
    )
    static let liquidStrokeStrong = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.95),
        dark: UIColor.white.withAlphaComponent(0.24)
    )
    static let liquidStrokeSoft = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.26),
        dark: UIColor.white.withAlphaComponent(0.08)
    )
    static let tabBarSurface = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.78),
        dark: UIColor(hex: "1B1C22").withAlphaComponent(0.86)
    )
    static let tabSelectionSurface = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.88),
        dark: UIColor(hex: "2A2B34").withAlphaComponent(0.92)
    )
    static let shadow = Color.dynamic(
        light: UIColor.black.withAlphaComponent(0.10),
        dark: UIColor.black.withAlphaComponent(0.48)
    )

    static let bottomTabBarHeight: CGFloat = 64
    static let bottomTabBarBottomPadding: CGFloat = -14
    static let guideComposerTabGap: CGFloat = 36
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

extension View {
    func glassPanel(cornerRadius: CGFloat = 24) -> some View {
        modifier(GlassPanelModifier(cornerRadius: cornerRadius))
    }

    func floatingLiquidPanel(cornerRadius: CGFloat = 28) -> some View {
        background(.regularMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .background(AppTheme.liquidOverlay, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(
                        LinearGradient(
                            colors: [
                                AppTheme.liquidStrokeStrong,
                                AppTheme.liquidStrokeSoft,
                                AppTheme.primary.opacity(0.20)
                            ],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ),
                        lineWidth: 1
                    )
            )
            .shadow(color: AppTheme.shadow, radius: 22, y: 12)
            .shadow(color: AppTheme.primary.opacity(0.10), radius: 18, y: 3)
    }
}
