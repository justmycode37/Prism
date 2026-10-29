import SwiftUI

struct ConsoleCard<Content: View>: View {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    let title: String?
    let caption: String?
    @ViewBuilder let content: Content

    init(_ title: String? = nil, caption: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.caption = caption
        self.content = content()
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let title {
                HStack {
                    Text(title).font(.system(size: 14, weight: .semibold))
                    Spacer()
                    if let caption {
                        Text(caption).font(.system(size: 10, weight: .medium, design: .monospaced)).foregroundStyle(.secondary)
                    }
                }
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 14).padding(.horizontal, 4)

    }
}

struct ConsoleMetric: View {
    let title: String
    let value: String
    let detail: String
    var body: some View {
        ConsoleCard {
            VStack(alignment: .leading, spacing: 12) {
                Text(title).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                Text(value).font(.system(size: 25, weight: .medium, design: .rounded)).monospacedDigit()
                    .lineLimit(1).minimumScaleFactor(0.65)
                Text(detail).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
            }
        }
    }
}

struct StatusPill: View {
    let status: String
    var color: Color {
        switch status.lowercased() {
        case "running", "working", "configured", "active", "completed", "passed", "accepted", "eligible", "paper", "paper_eligible": .mint
        case "error", "failed", "rejected": .orange
        case "paused", "budget_limited", "waiting", "queued": .secondary
        default: .secondary
        }
    }
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 5, height: 5)
            Text(PrismFormat.label(status)).lineLimit(1)
        }
        .font(.system(size: 10, weight: .medium)).padding(.horizontal, 9).padding(.vertical, 6)
        .accessibilityLabel("Status: \(PrismFormat.label(status))")
    }
}

struct ConsoleEmptyState: View {
    let symbol: String
    let title: String
    let message: String
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol).font(.system(size: 27, weight: .ultraLight)).foregroundStyle(.secondary)
            Text(title).font(.system(size: 15, weight: .medium))
            Text(message).font(.system(size: 12)).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).frame(maxWidth: 380)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 35)
    }
}

struct EventRow: View {
    let event: RuntimeEvent
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: event.level == "error" ? "exclamationmark.circle" : "circle.fill")
                .font(.system(size: event.level == "error" ? 13 : 5))
                .foregroundStyle(event.level == "error" ? Color.orange : .secondary).frame(width: 14, height: 16)
            VStack(alignment: .leading, spacing: 4) {
                Text(event.message).font(.system(size: 12)).textSelection(.enabled)
                Text(PrismFormat.label(event.source)).font(.system(size: 10)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            Text(PrismFormat.time(event.timestamp)).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
        }
        .padding(.vertical, 7)
    }
}
