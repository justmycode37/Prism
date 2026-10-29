import SwiftUI

struct PrismActivityView: View {
    @ObservedObject var runtime: RuntimeClient
    @State private var query = ""
    private var events: [RuntimeEvent] {
        (runtime.state?.events ?? []).filter {
            query.isEmpty || $0.message.localizedCaseInsensitiveContains(query) || $0.source.localizedCaseInsensitiveContains(query)
        }
    }
    var body: some View {
        VStack(spacing: 18) {
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search", text: $query).textFieldStyle(.plain)
                    .accessibilityLabel("Search activity")
                if !query.isEmpty {
                    Button { query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                        .buttonStyle(.plain).accessibilityLabel("Clear search")
                }
            }
            .font(.system(size: 12)).padding(.vertical, 8)
            ConsoleCard {
                if events.isEmpty {
                    Text(query.isEmpty ? "No events yet." : "No matching events.").font(.system(size: 12)).foregroundStyle(.secondary)
                } else {
                    LazyVStack(spacing: 8) { ForEach(events) { EventRow(event: $0) } }
                }
            }
        }
    }
}
