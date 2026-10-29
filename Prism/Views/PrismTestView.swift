import SwiftUI

struct PrismTestView: View {
    @ObservedObject var runtime: RuntimeClient
    @State private var statusFilter = "All results"
    private var strategies: [StrategyResult] {
        (runtime.state?.strategies ?? []).filter { statusFilter == "All results" || $0.status == statusFilter }
    }
    private var statuses: [String] { ["All results"] + Set(runtime.state?.strategies.map(\.status) ?? []).sorted() }
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Picker("Result status", selection: $statusFilter) {
                ForEach(statuses, id: \.self) { status in Text(PrismFormat.label(status)).tag(status) }
            }.labelsHidden().frame(width: 145)
            if strategies.isEmpty {
                Text("No test results yet.").font(.system(size: 12)).foregroundStyle(.secondary)
            }
            ForEach(strategies) { strategy in
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 16) {
                        HStack(spacing: 28) {
                            metric("Net return", PrismFormat.percent(strategy.netReturn))
                            metric("Sharpe", PrismFormat.number(strategy.sharpe))
                            metric("Drawdown", PrismFormat.percent(strategy.maxDrawdown))
                            metric("Trades", strategy.trades.map(String.init) ?? "—")
                        }
                        Text(strategy.reason).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                        Text("\(PrismFormat.label(strategy.family)) · \(PrismFormat.time(strategy.createdAt))")
                            .font(.system(size: 10)).foregroundStyle(.tertiary)
                    }.padding(.vertical, 14).padding(.leading, 18)
                } label: {
                    HStack(spacing: 12) {
                        Text(strategy.name).font(.system(size: 13))
                        Text(strategy.symbol).font(.system(size: 10)).foregroundStyle(.tertiary)
                        Spacer()
                        StatusPill(status: strategy.status)
                    }.padding(.vertical, 5)
                }
            }
        }.padding(.horizontal, 4)
    }
    private func metric(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 15, design: .rounded)).monospacedDigit()
        }
    }
}
