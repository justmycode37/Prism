import SwiftUI

struct PrismWordmark: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text("P")
            Text("R")
            InvertedPrism()
                .stroke(style: StrokeStyle(lineWidth: 1.45, lineCap: .round, lineJoin: .round))
                .frame(width: 39, height: 34.08)
                .alignmentGuide(.firstTextBaseline) { $0[.bottom] }
            Text("S")
            Text("M")
        }
            .font(PrismTypography.wordmark)
            .foregroundStyle(colorScheme == .dark ? .white.opacity(0.94) : Color(red: 0.12, green: 0.15, blue: 0.2))
            .fixedSize()
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Prism")
            .accessibilityAddTraits(.isHeader)
    }
}

/// The downward-pointing, outlined prism replaces the letter I.
private struct InvertedPrism: Shape {
    func path(in rect: CGRect) -> Path {
        let bounds = rect.insetBy(dx: 0.725, dy: 0.725)
        return Path { path in
            path.move(to: CGPoint(x: bounds.minX, y: bounds.minY))
            path.addLine(to: CGPoint(x: bounds.maxX, y: bounds.minY))
            path.addLine(to: CGPoint(x: bounds.midX, y: bounds.maxY))
            path.closeSubpath()
        }
    }
}
