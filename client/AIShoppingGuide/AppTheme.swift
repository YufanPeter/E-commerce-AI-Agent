import SwiftUI
import UIKit

struct AppTheme {
    static let primary = Color.dynamic(lightHex: "0F8F8C", darkHex: "5EEAD4")
    static let secondary = Color.dynamic(lightHex: "E6F5F2", darkHex: "122E30")
    static let softBlue = Color.dynamic(lightHex: "EDF5FF", darkHex: "142536")
    static let softPurple = Color.dynamic(lightHex: "EAF7F3", darkHex: "142B2D")
    static let background = Color.dynamic(lightHex: "F5F7F8", darkHex: "0D1113")
    static let surface = Color.dynamic(lightHex: "FEFFFF", darkHex: "171D20")
    static let textPrimary = Color.dynamic(lightHex: "182124", darkHex: "F2F7F7")
    static let textSecondary = Color.dynamic(lightHex: "627174", darkHex: "9FB0B3")
    static let success = Color.dynamic(lightHex: "22C55E", darkHex: "4ADE80")
    static let error = Color.dynamic(lightHex: "EF4444", darkHex: "FF6B6B")
    static let border = Color.dynamic(
        light: UIColor(hex: "123133").withAlphaComponent(0.09),
        dark: UIColor.white.withAlphaComponent(0.13)
    )
    static let liquidOverlay = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.28),
        dark: UIColor.white.withAlphaComponent(0.07)
    )
    static let liquidStrokeStrong = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.88),
        dark: UIColor.white.withAlphaComponent(0.22)
    )
    static let liquidStrokeSoft = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.26),
        dark: UIColor.white.withAlphaComponent(0.08)
    )
    static let tabBarSurface = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.82),
        dark: UIColor(hex: "172024").withAlphaComponent(0.88)
    )
    static let tabSelectionSurface = Color.dynamic(
        light: UIColor.white.withAlphaComponent(0.92),
        dark: UIColor(hex: "223035").withAlphaComponent(0.92)
    )
    static let shadow = Color.dynamic(
        light: UIColor(hex: "092C2E").withAlphaComponent(0.10),
        dark: UIColor.black.withAlphaComponent(0.46)
    )
    static let accentGlow = Color.dynamic(
        light: UIColor(hex: "0F8F8C").withAlphaComponent(0.14),
        dark: UIColor(hex: "5EEAD4").withAlphaComponent(0.12)
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
            .shadow(color: AppTheme.shadow.opacity(0.42), radius: 14, y: 7)
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
        background(.regularMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .background(AppTheme.liquidOverlay, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(
                        LinearGradient(
                            colors: [
                                AppTheme.liquidStrokeStrong,
                                AppTheme.liquidStrokeSoft,
                                AppTheme.primary.opacity(0.14)
                            ],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ),
                        lineWidth: 1
                    )
            )
            .shadow(color: AppTheme.shadow.opacity(0.72), radius: 18, y: 9)
            .shadow(color: AppTheme.accentGlow, radius: 14, y: 2)
    }
}

extension ButtonStyle where Self == TactileButtonStyle {
    static var tactile: TactileButtonStyle { TactileButtonStyle() }
}
