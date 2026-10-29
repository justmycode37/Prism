import SwiftUI

/// Native glass supplies the lens edge. Never draw a crisp outline over it.
struct GlassSurface: View {
    let milkiness: Double
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    private var dark: Bool { colorScheme == .dark }
    private var shape: RoundedRectangle {
        RoundedRectangle(cornerRadius: PrismTheme.cornerRadius, style: .continuous)
    }

    var body: some View {
        ZStack {
            if reduceTransparency {
                shape.fill(dark ? Color(red: 0.1, green: 0.12, blue: 0.16) : Color(red: 0.94, green: 0.95, blue: 0.97))
            } else {
                // Soft thickness and grazing light, with a transparent center.
                shape.fill((dark ? Color.black : Color.white).opacity(milkiness))
                shape.strokeBorder(
                    LinearGradient(colors: [.white.opacity(0.2), .clear, .white.opacity(0.13)], startPoint: .topLeading, endPoint: .bottomTrailing),
                    lineWidth: 10
                )
                .blur(radius: 9)
                .mask(shape)
            }

            if contrast == .increased {
                shape.inset(by: 1).strokeBorder(.primary.opacity(0.7), lineWidth: 2)
            }
        }
        .clipShape(shape)
        .accessibilityHidden(true)
        .allowsHitTesting(false)
    }
}
