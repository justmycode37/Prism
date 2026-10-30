import SwiftUI

/// Frosted content surfaces share one radius, spacing and accessible fallback.
/// Native liquid glass stays on controls; content uses material to remain legible.
private struct ConsoleGlass: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: 22, style: .continuous)
        content
            .background {
                if reduceTransparency {
                    shape.fill(colorScheme == .dark ? Color(white: 0.16) : Color(white: 0.96))
                } else {
                    shape.fill(.ultraThinMaterial).opacity(0.6)
                    shape.fill(.white.opacity(colorScheme == .dark ? 0.025 : 0.12))
                }
            }
            .overlay {
                shape.strokeBorder(
                    contrast == .increased ? AnyShapeStyle(.primary.opacity(0.6)) :
                        AnyShapeStyle(LinearGradient(colors: [.white.opacity(0.24), .white.opacity(0.025), .white.opacity(0.1)],
                                                    startPoint: .topLeading, endPoint: .bottomTrailing)),
                    lineWidth: contrast == .increased ? 1.5 : 0.65
                ).allowsHitTesting(false)
            }
    }
}

struct ConsoleCard<Content: View>: View {
    let title: String?
    let caption: String?
    @ViewBuilder let content: Content

    init(_ title: String? = nil, caption: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.caption = caption
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let title {
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        heading(title)
                        Spacer(minLength: 0)
                        captionText
                    }
                    VStack(alignment: .leading, spacing: 5) {
                        heading(title)
                        captionText
                    }
                }
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .modifier(ConsoleGlass())
    }

    private func heading(_ title: String) -> some View {
        Text(title).font(.system(size: 13, weight: .medium)).accessibilityAddTraits(.isHeader)
    }

    @ViewBuilder private var captionText: some View {
        if let caption {
            Text(caption).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }
}

struct ConsoleMetricGrid<Content: View>: View {
    @ViewBuilder let content: Content
    var body: some View {
        ConsoleMetricLayout { content }
    }
}

/// Uses the available width, rather than the ideal width of long currency labels.
/// A single layout also preserves each metric's identity when the window resizes.
private struct ConsoleMetricLayout: Layout {
    private let spacing: CGFloat = 12

    private func measurements(width: CGFloat, subviews: Subviews) -> (columns: Int, cellWidth: CGFloat, cellHeight: CGFloat) {
        let columns = max(1, min(subviews.count, Int((width + spacing) / (130 + spacing))))
        let cellWidth = max(0, (width - CGFloat(columns - 1) * spacing) / CGFloat(columns))
        let cellHeight = subviews.map { $0.sizeThatFits(.init(width: cellWidth, height: nil)).height }.max() ?? 0
        return (columns, cellWidth, cellHeight)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard !subviews.isEmpty else { return .zero }
        let width = proposal.width.flatMap { $0.isFinite ? max(0, $0) : nil } ?? 640
        let layout = measurements(width: width, subviews: subviews)
        let rows = (subviews.count + layout.columns - 1) / layout.columns
        return CGSize(width: width, height: CGFloat(rows) * layout.cellHeight + CGFloat(rows - 1) * spacing)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let layout = measurements(width: bounds.width, subviews: subviews)
        for (index, subview) in subviews.enumerated() {
            subview.place(at: CGPoint(x: bounds.minX + CGFloat(index % layout.columns) * (layout.cellWidth + spacing),
                                      y: bounds.minY + CGFloat(index / layout.columns) * (layout.cellHeight + spacing)),
                          anchor: .topLeading,
                          proposal: .init(width: layout.cellWidth, height: layout.cellHeight))
        }
    }
}

struct ConsoleMetric: View {
    let title: String
    let value: String
    let detail: String
    var body: some View {
        ConsoleCard {
            VStack(alignment: .leading, spacing: 9) {
                Text(title).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                Text(value).font(.system(size: 25, weight: .medium, design: .rounded)).monospacedDigit()
                    .lineLimit(1).minimumScaleFactor(0.65)
                Text(detail).font(.system(size: 10)).foregroundStyle(.secondary)
                    .lineLimit(2, reservesSpace: true)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .accessibilityElement(children: .combine)
    }
}

struct ConsoleIcon: View {
    let symbol: String
    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 16, weight: .regular))
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(.secondary)
            .frame(width: 32, height: 32)
            .background(.primary.opacity(0.045), in: .circle)
            .accessibilityHidden(true)
    }
}

struct StatusPill: View {
    let status: String
    var color: Color {
        switch status.lowercased() {
        case "running", "working", "configured", "active", "completed", "passed", "accepted", "eligible", "paper", "paper_eligible": .mint
        case "error", "failed", "rejected", "blocked", "halted": .orange
        default: .secondary
        }
    }
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 5, height: 5)
            Text(PrismFormat.label(status)).lineLimit(1)
        }
        .font(.system(size: 10, weight: .medium))
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(.primary.opacity(0.045), in: .capsule)
        .accessibilityLabel("Status: \(PrismFormat.label(status))")
    }
}

struct ConsoleEmptyState: View {
    let symbol: String
    let title: String
    let message: String
    var body: some View {
        VStack(spacing: 10) {
            ConsoleIcon(symbol: symbol)
            Text(title).font(.system(size: 13, weight: .medium))
            Text(message).font(.system(size: 11)).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).frame(maxWidth: 340)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 20)
    }
}

struct EventRow: View {
    let event: RuntimeEvent
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: event.level == "error" ? "exclamationmark.circle" : "circle")
                .font(.system(size: 12, weight: .regular))
                .foregroundStyle(event.level == "error" ? Color.orange : .secondary)
                .frame(width: 16, height: 16).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                Text(event.message).font(.system(size: 12)).textSelection(.enabled)
                Text(PrismFormat.label(event.source)).font(.system(size: 10)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            Text(PrismFormat.time(event.timestamp)).font(.system(size: 10)).monospacedDigit().foregroundStyle(.secondary)
        }
        .padding(.vertical, 7)
    }
}
