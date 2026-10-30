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
            if let research = runtime.state?.research {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Prospective research").font(.system(size: 15, weight: .medium))
                    Text("Historical results are development evidence. Frozen candidates accumulate future observations in separate shadow accounts.")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                    if let count = research.experimentCount {
                        Text("\(count) registered experiments").font(.system(size: 12)).monospacedDigit()
                    }
                    if let cost = research.operatingCosts?.totalCostUsd ?? research.operatingCosts?.modelCostUsd {
                        Text("Estimated model cost: \(PrismFormat.money(cost))")
                            .font(.system(size: 12)).monospacedDigit()
                    }
                    ForEach((research.failureReasons ?? [:]).keys.sorted(), id: \.self) { reason in
                        Text("\(research.failureReasons?[reason] ?? 0) · \(reason)")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    ForEach(research.coverage?.gaps ?? [], id: \.self) { gap in
                        Text(gap).font(.system(size: 11)).foregroundStyle(.orange)
                    }
                    ForEach(research.forwardReports ?? []) { report in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(report.experimentId).lineLimit(1).truncationMode(.middle)
                                Spacer()
                                Text(PrismFormat.label(report.status ?? "Awaiting observations"))
                            }
                            Text("\(report.sessions ?? 0) future sessions · Net \(PrismFormat.percent(report.netReturn)) · Excess \(PrismFormat.percent(report.excessReturn))")
                                .monospacedDigit()
                            if let pnl = report.operatingPnlUsd {
                                Text("After operating costs: \(PrismFormat.money(pnl))")
                            }
                            Text((report.reasons ?? []).joined(separator: "; "))
                                .foregroundStyle(.secondary).textSelection(.enabled)
                        }.font(.system(size: 11)).padding(.vertical, 5)
                    }
                }
            }
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
                            metric(strategy.phase == "development" ? "Development return" : "Historical return", PrismFormat.percent(strategy.netReturn))
                            metric("Sharpe", PrismFormat.number(strategy.sharpe))
                            metric("Drawdown", PrismFormat.percent(strategy.maxDrawdown))
                            metric("Trades", strategy.trades.map(String.init) ?? "—")
                        }
                        Text(strategy.reason).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                        Text("\(PrismFormat.label(strategy.family)) · \(PrismFormat.label(strategy.phase ?? "development")) · \(PrismFormat.time(strategy.createdAt))")
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
