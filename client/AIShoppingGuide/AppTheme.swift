import SwiftUI

struct AppTheme {
    static let primary = Color(hex: "6C5CE7")
    static let secondary = Color(hex: "F2F3FF")
    static let softBlue = Color(hex: "F4F0FF")
    static let softPurple = Color(hex: "F2F3FF")
    static let background = Color(hex: "F7F8FA")
    static let surface = Color.white
    static let textPrimary = Color(hex: "1D1E20")
    static let textSecondary = Color(hex: "637280")
    static let success = Color(hex: "22C55E")
    static let error = Color(hex: "EF4444")
    static let border = Color.black.opacity(0.08)
}

extension Color {
    init(hex: String) {
        let scanner = Scanner(string: hex)
        var rgb: UInt64 = 0
        scanner.scanHexInt64(&rgb)
        self.init(
            red: Double((rgb >> 16) & 0xFF) / 255,
            green: Double((rgb >> 8) & 0xFF) / 255,
            blue: Double(rgb & 0xFF) / 255
        )
    }
}

struct GlassPanelModifier: ViewModifier {
    let cornerRadius: CGFloat

    func body(content: Content) -> some View {
        content
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .stroke(.white.opacity(0.65), lineWidth: 1)
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
}
