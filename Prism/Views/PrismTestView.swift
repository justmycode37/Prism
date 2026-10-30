import SwiftUI

struct PrismTestView: View {
    @ObservedObject var runtime: RuntimeClient
    @State private var statusFilter = "All results"

    private var allStrategies: [StrategyResult] { runtime.state?.strategies ?? [] }
    private var strategies: [StrategyResult] { allStrategies.filter { statusFilter == "All results" || $0.status == statusFilter } }
    private var statuses: [String] { ["All results"] + Set(allStrategies.map(\.status)).sorted() }
    private var research: ResearchStatus? { runtime.state?.research }
    private var forwardReports: [ForwardResearchReport] { research?.forwardReports ?? [] }
    private var evaluatedCount: Int { allStrategies.filter { $0.netReturn != nil || $0.sharpe != nil || $0.trades != nil }.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            ConsoleMetricGrid {
                ConsoleMetric(title: "Evaluated", value: runtime.state == nil ? "—" : "\(evaluatedCount)", detail: runtime.state == nil ? "Awaiting runtime" : "\(allStrategies.count) candidates · historical")
                ConsoleMetric(title: "Forward reports", value: research?.forwardReports == nil ? "—" : "\(forwardReports.count)", detail: "Prospective evidence")
                ConsoleMetric(title: "Research spend", value: costValue, detail: research?.operatingCosts?.totalCostUsd != nil ? "Reported total cost" : "Model estimate only")
            }

            ConsoleCard("Prospective evidence", caption: "Forward observations") {
                Text("Future observations from frozen candidates.")
                    .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if forwardReports.isEmpty {
                    Text("No prospective observations reported yet.").font(.system(size: 12)).foregroundStyle(.secondary)
                } else {
                    ForEach(forwardReports) { report in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack(spacing: 8) {
                                Text(report.experimentId).font(.system(size: 11, weight: .medium)).lineLimit(1).truncationMode(.middle)
                                Spacer(minLength: 4)
                                StatusPill(status: report.status ?? "Awaiting observations")
                            }
                            LazyVGrid(columns: [GridItem(.adaptive(minimum: 130), alignment: .leading)], alignment: .leading, spacing: 12) {
                                resultValue("Sessions", report.sessions.map(String.init) ?? "—")
                                resultValue("Net return", report.netReturn.map(PrismFormat.percent) ?? "—")
                                resultValue("Excess return", report.excessReturn.map(PrismFormat.percent) ?? "—")
                                if let pnl = report.operatingPnlUsd { resultValue("After costs", PrismFormat.money(pnl)) }
                            }
                            if let reasons = report.reasons, !reasons.isEmpty {
                                Label(reasons.joined(separator: "; "), systemImage: "info.circle")
                                    .font(.system(size: 10)).foregroundStyle(.secondary).textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }.padding(.vertical, 9)
                        if report.id != forwardReports.last?.id { Divider().opacity(0.5) }
                    }
                }
            }

            ConsoleCard("Development results", caption: "Historical · exploratory") {
                Text("Development evidence · prospective qualification is separate.")
                    .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if !statuses.isEmpty {
                    Picker("Result status", selection: $statusFilter) {
                        ForEach(statuses, id: \.self) { status in Text(PrismFormat.label(status)).tag(status) }
                    }.labelsHidden().pickerStyle(.menu)
                }
                if strategies.isEmpty {
                    ConsoleEmptyState(symbol: "chart.bar", title: "No development results yet", message: statusFilter == "All results" ? "Strategy evaluations will appear here when available." : "No results match this status filter.")
                } else {
                    ForEach(strategies) { strategy in
                        developmentRow(strategy)
                        if strategy.id != strategies.last?.id { Divider().opacity(0.5) }
                    }
                }
            }

            diagnostics
        }
        .onChange(of: statuses) { _, available in
            if !available.contains(statusFilter) { statusFilter = "All results" }
        }
    }

    private var costValue: String {
        guard let cost = research?.operatingCosts?.totalCostUsd ?? research?.operatingCosts?.modelCostUsd else { return "—" }
        return PrismFormat.money(cost)
    }

    private func developmentRow(_ strategy: StrategyResult) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(strategy.name).font(.system(size: 13, weight: .medium)).lineLimit(1)
                    Text("\(strategy.symbol) · \(PrismFormat.label(strategy.family)) · \(PrismFormat.label(strategy.phase ?? "development"))")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                StatusPill(status: strategy.status)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 130), alignment: .leading)], alignment: .leading, spacing: 12) {
                resultValue("Return", strategy.netReturn.map(PrismFormat.percent) ?? "—")
                resultValue("Sharpe", strategy.sharpe.map(PrismFormat.number) ?? "—")
                resultValue("Drawdown", strategy.maxDrawdown.map(PrismFormat.percent) ?? "—")
                resultValue("Trades", strategy.trades.map(String.init) ?? "—")
            }
            DisclosureGroup("Evaluation details") {
                VStack(alignment: .leading, spacing: 7) {
                    Text(strategy.reason).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Text("Created · \(PrismFormat.time(strategy.createdAt))")
                        Spacer()
                        if let experimentId = strategy.experimentId { Text("Experiment · \(experimentId)").lineLimit(1).truncationMode(.middle) }
                    }.foregroundStyle(.tertiary)
                    if let agentId = strategy.agentId, let role = runtime.state?.agents.first(where: { $0.id == agentId }) {
                        Text("Role · \(role.name)").foregroundStyle(.tertiary)
                    }
                }.font(.system(size: 10)).foregroundStyle(.secondary).padding(.top, 7)
            }.font(.system(size: 11))
        }.padding(.vertical, 10)
    }

    private func resultValue(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 12, weight: .medium)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.75)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private var diagnostics: some View {
        let failures = research?.failureReasons ?? [:]
        let gaps = research?.coverage?.gaps ?? []
        let costs = research?.operatingCosts
        if !failures.isEmpty || !gaps.isEmpty || costs != nil || research?.experimentCount != nil || research?.coverage?.survivorshipSafe == false {
            ConsoleCard("Run diagnostics", caption: "Coverage & costs") {
                if !gaps.isEmpty {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(gaps, id: \.self) { gap in
                            Label(gap, systemImage: "exclamationmark.triangle")
                                .font(.system(size: 11)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                if research?.coverage?.survivorshipSafe == false {
                    Label("The research universe is not survivorship safe.", systemImage: "exclamationmark.triangle")
                        .font(.system(size: 11)).foregroundStyle(.orange)
                }
                if let costs {
                    Text("\(costs.calls.map(String.init) ?? "—") model calls · model \(costs.modelCostUsd.map { PrismFormat.money($0) } ?? "—") · total \(costs.totalCostUsd.map { PrismFormat.money($0) } ?? "—")")
                        .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                }
                if let count = research?.experimentCount {
                    Text("\(count) registered experiments").font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                }
                if !failures.isEmpty {
                    DisclosureGroup("Failure reasons (\(failures.values.reduce(0, +)))") {
                        ForEach(failures.keys.sorted(), id: \.self) { reason in
                            HStack(alignment: .top) {
                                Text(reason).fixedSize(horizontal: false, vertical: true)
                                Spacer(minLength: 10)
                                Text("\(failures[reason] ?? 0)").monospacedDigit()
                            }.font(.system(size: 11)).foregroundStyle(.secondary).padding(.vertical, 4)
                        }.padding(.top, 5)
                    }.font(.system(size: 11))
                }
            }
        }
    }
}
