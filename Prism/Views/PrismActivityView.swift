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
        ConsoleCard("Event log", caption: "\(events.count) EVENTS") {
            HStack(spacing: 8) {
                ConsoleIcon(symbol: "magnifyingglass")
                TextField("Search events", text: $query).textFieldStyle(.plain)
                    .font(.system(size: 12)).accessibilityLabel("Search activity")
                if !query.isEmpty {
                    Button { query = "" } label: { ConsoleIcon(symbol: "xmark") }
                        .buttonStyle(.plain).accessibilityLabel("Clear search")
                }
            }.padding(.vertical, 4)

            if events.isEmpty {
                Text(query.isEmpty ? "No events yet." : "No matching events.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            } else {
                LazyVStack(spacing: 4) {
                    ForEach(events) { EventRow(event: $0) }
                }
            }
        }
    }
}
