import SwiftUI

struct PrismWorkspacePicker: View {
    let onSelect: (PrismWorkspace) -> Void

    var body: some View {
        VStack(spacing: 26) {
            VStack(spacing: 12) {
                PrismWordmark().scaleEffect(0.68).frame(height: 38)
                Text("Choose your workspace")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            HStack(spacing: 16) {
                WorkspaceChoice(workspace: .singleTrader,
                                detail: "One trader. One market.",
                                caption: "SOLUSDT perpetual") { onSelect(.singleTrader) }
                WorkspaceChoice(workspace: .agentSwarm,
                                detail: "Research, test and execute.",
                                caption: "A coordinated team of agents") { onSelect(.agentSwarm) }
            }
            Text("Two workspaces. One Prism.")
                .font(.system(size: 10)).foregroundStyle(.tertiary)
        }
        .frame(maxWidth: 600)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityIdentifier("workspace-picker")
    }
}

private struct WorkspaceChoice: View {
    let workspace: PrismWorkspace
    let detail: String
    let caption: String
    let action: () -> Void
    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            ConsoleCard {
                VStack(alignment: .leading, spacing: 19) {
                    Image(systemName: workspace.symbol)
                        .font(.system(size: 24, weight: .ultraLight))
                        .frame(height: 28).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 7) {
                        Text(workspace.title)
                            .font(.system(size: 17, weight: .medium, design: .rounded))
                        Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    HStack(spacing: 8) {
                        Text(caption).font(.system(size: 10)).foregroundStyle(.tertiary)
                        Spacer(minLength: 0)
                        Image(systemName: "arrow.up.right")
                            .font(.system(size: 12, weight: .regular)).foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 144, alignment: .leading)
            }
            .background(.primary.opacity(isHovered ? 0.045 : 0), in: .rect(cornerRadius: 22))
            .contentShape(.rect(cornerRadius: 22))
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
        .help("Open \(workspace.title)")
        .accessibilityLabel(workspace.title)
        .accessibilityHint("\(detail) \(caption)")
        .accessibilityIdentifier("choose-\(workspace.rawValue)")
    }
}
