import SwiftUI

struct PrismExecutionView: View {
    @ObservedObject var runtime: RuntimeClient
    @State private var activityExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let operations = runtime.state?.operations {
                PrismOperationsView(operations: operations)
            }
            PrismPortfolioView(runtime: runtime)
            DisclosureGroup("Activity", isExpanded: $activityExpanded) {
                PrismActivityView(runtime: runtime).padding(.top, 10)
            }
        }
    }
}

/// Bounded backend projection keeps the console aligned with the durable journal.
private struct PrismOperationsView: View {
    let operations: OperationalStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ConsoleCard("Operations", caption: "BYBIT · \(operations.mode.uppercased())") {
                HStack(alignment: .center, spacing: 10) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(operations.enabled ? "Continuous operations" : "Execution disabled")
                            .font(.system(size: 16, weight: .medium, design: .rounded))
                        Text("Next wake · \(operations.nextWakeAt.map { PrismFormat.time($0) } ?? "Awaiting a plan")")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 4)
                    StatusPill(status: operations.riskStatus ?? "waiting")
                }

                if let rationale = operations.rationale {
                    Text(rationale).font(.system(size: 11)).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            ConsoleMetricGrid {
                ConsoleMetric(title: "Gross exposure", value: money(operations.grossExposure), detail: "Operations")
                ConsoleMetric(title: "Realized P&L", value: money(operations.realizedPnl), detail: "Operations")
                ConsoleMetric(title: "Unrealized P&L", value: money(operations.unrealizedPnl), detail: "Operations")
            }

            ConsoleCard("Estimated model costs", caption: "\(operations.mode.uppercased()) · USD") {
                Text("Estimated usage against component limits")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                if operations.componentCosts.isEmpty {
                    Text("No component cost data recorded.").font(.system(size: 11)).foregroundStyle(.secondary)
                } else {
                    ForEach(operations.componentCosts) { cost in
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(spacing: 6) {
                                Text(PrismFormat.label(cost.id)).font(.system(size: 12, weight: .medium))
                                Spacer(minLength: 4)
                                Text("\(cost.calls) requests").font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                            Text("Spent \(PrismFormat.money(cost.spentUsd)) · Reserved \(PrismFormat.money(cost.reservedUsd)) · Limit \(PrismFormat.money(cost.limitUsd))")
                                .font(.system(size: 10)).foregroundStyle(.secondary).monospacedDigit()
                                .fixedSize(horizontal: false, vertical: true)
                        }.padding(.vertical, 4)
                    }
                }
            }
            if !operations.sections.isEmpty {
              ConsoleCard("Operational details") {
                ForEach(operations.sections) { section in
                    DisclosureGroup("\(section.title) · \(section.items.count)") {
                        VStack(alignment: .leading, spacing: 10) {
                            if section.items.isEmpty {
                                Text("No recorded items.").foregroundStyle(.secondary)
                            }
                            ForEach(section.items) { item in
                                HStack(alignment: .top, spacing: 8) {
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(item.title).fontWeight(.medium)
                                        Text(item.detail).foregroundStyle(.secondary).textSelection(.enabled)
                                        if let source = item.sourceUrl, let url = URL(string: source), url.scheme == "https" {
                                            Link("Source", destination: url).font(.system(size: 10))
                                        }
                                    }
                                    Spacer(minLength: 4)
                                    VStack(alignment: .trailing, spacing: 4) {
                                        if let status = item.status { StatusPill(status: status) }
                                        if let time = item.time { Text(PrismFormat.time(time)).foregroundStyle(.tertiary) }
                                    }
                                }.font(.system(size: 11))
                            }
                        }.padding(.top, 10)
                    }.font(.system(size: 12, weight: .medium))
                }
              }
            }
        }
    }

    private func money(_ value: Double?) -> String {
        value.map { PrismFormat.money($0) } ?? "—"
    }
}
