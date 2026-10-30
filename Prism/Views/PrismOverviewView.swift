import SwiftUI

struct PrismOverviewView: View {
    @ObservedObject var runtime: RuntimeClient
    let availableHeight: CGFloat
    @State private var selectedWindow = "all"
    @State private var showsAllResults = false

    private var agentsAreRunning: Bool {
        guard let status = runtime.state?.runtime.status else { return false }
        return status != "paused"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            summary
            runtimeControl
            ConsoleCard("Strategy results", caption: "Historical development") {
                leaderboardContent
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .onChange(of: runtime.state?.research?.leaderboard?.testWindows ?? []) { _, windows in
            if selectedWindow != "all" && !windows.contains(selectedWindow) { selectedWindow = "all" }
        }
    }

    private var summary: some View {
        ConsoleMetricGrid {
            ConsoleMetric(title: "Paper P&L",
                          value: runtime.state?.portfolio.totalPnl.map { PrismFormat.money($0) } ?? "—",
                          detail: "\(PrismFormat.percent(runtime.state?.portfolio.totalReturn)) · all time")
            ConsoleMetric(title: "Model cost today",
                          value: runtime.state.map { PrismFormat.money($0.budget.spentTodayUsd) } ?? "—",
                          detail: runtime.state.map { "\($0.budget.callsToday) requests · estimated" } ?? "Awaiting runtime")
            ConsoleMetric(title: "Agents",
                          value: runtime.state.map { String($0.agents.count) } ?? "—",
                          detail: runtime.state.map { state in
                              let working = state.agents.filter { ["working", "running", "active"].contains($0.status) }.count
                              return "\(working) active · \(Set(state.agents.map { $0.roleId ?? $0.id }).count) roles"
                          } ?? "Awaiting runtime")
        }
    }

    @ViewBuilder private var leaderboardContent: some View {
        if let leaderboard = runtime.state?.research?.leaderboard {
            let selectedRows = leaderboard.rankedRows.filter {
                selectedWindow == "all" || !leaderboard.testWindows.contains(selectedWindow) || $0.testWindow == selectedWindow
            }
            let rows = showsAllResults ? selectedRows : Array(selectedRows.prefix(5))
            if rows.isEmpty {
                ConsoleEmptyState(symbol: PrismPage.overview.symbol, title: "No development results yet",
                                  message: "Completed strategy evaluations will appear here.")
            } else {
                if !leaderboard.testWindows.isEmpty {
                    Picker("Test period", selection: $selectedWindow) {
                        Text("All test periods").tag("all")
                        ForEach(leaderboard.testWindows, id: \.self) { window in Text(window).tag(window) }
                    }
                    .pickerStyle(.menu).font(.system(size: 11))
                    .help("Compare returns over matching test dates.")
                }
                VStack(spacing: 12) {
                    ForEach(rows.indices, id: \.self) { index in
                        leaderboardRow(rows[index], rank: index + 1)
                        if index < rows.count - 1 { Divider().opacity(0.35) }
                    }
                }
                HStack {
                    Text("\(leaderboard.ranked ?? 0) measured · \(leaderboard.total ?? rows.count) candidates")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    Spacer(minLength: 8)
                    if selectedRows.count > 5 {
                        Button(showsAllResults ? "Show top 5" : "Show all \(selectedRows.count)") {
                            showsAllResults.toggle()
                        }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .medium))
                    }
                }
                Text("Cumulative development returns. Compare matching periods. Prospective qualification is separate.")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else {
            ConsoleEmptyState(symbol: PrismPage.overview.symbol, title: "Results unavailable",
                              message: "The current snapshot has no development leaderboard.")
        }
    }

    private func leaderboardRow(_ row: ResearchLeaderboardRow, rank: Int) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 12) {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 110), alignment: .leading)], alignment: .leading, spacing: 14) {
                    metric("Max drawdown", value: row.finiteMaxDrawdown.map { PrismFormat.percent(abs($0)) } ?? "—")
                    metric("Trades", value: row.trades.map(String.init) ?? "—")
                    metric("Phase", value: row.phase.map(PrismFormat.label) ?? "—")
                    metric("Qualification", value: qualificationLabel(row.qualification))
                }
                Text(row.testWindow ?? "Test dates unavailable")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .accessibilityLabel("Test period: \(row.testWindow ?? "unavailable")")
                ForEach(row.qualification?.reasons ?? [], id: \.self) { reason in
                    Text(reason).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }.padding(.top, 12).padding(.bottom, 4)
        } label: {
            HStack(spacing: 12) {
                Text(row.finiteNetReturn == nil ? "—" : String(format: "%02d", rank))
                    .font(.system(size: 11, weight: .medium, design: .rounded)).monospacedDigit()
                    .foregroundStyle(.secondary).frame(width: 24)
                VStack(alignment: .leading, spacing: 4) {
                    Text(nonempty(row.name, fallback: "Unnamed strategy"))
                        .font(.system(size: 12, weight: .medium)).lineLimit(1)
                    Text("\(nonempty(row.symbol, fallback: "Unknown symbol")) · \(qualificationLabel(row.qualification))")
                        .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
                VStack(alignment: .trailing, spacing: 3) {
                    Text(returnText(row.finiteNetReturn))
                        .font(.system(size: 17, weight: .medium, design: .rounded)).monospacedDigit()
                        .foregroundStyle(returnColor(row.finiteNetReturn)).lineLimit(1)
                    Text("Dev. return").font(.system(size: 9)).foregroundStyle(.secondary)
                }
            }.padding(.vertical, 3)
        }
        .tint(.secondary)
    }

    private func metric(_ label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 12, weight: .medium)).monospacedDigit()
        }
    }

    private func nonempty(_ value: String?, fallback: String) -> String {
        guard let value, !value.isEmpty else { return fallback }
        return value
    }

    private func returnText(_ value: Double?) -> String {
        guard let value else { return "—" }
        return (value > 0 ? "+" : "") + PrismFormat.percent(value)
    }

    private func returnColor(_ value: Double?) -> Color {
        guard let value, value != 0 else { return .secondary }
        return value > 0 ? .mint : .orange
    }

    private func qualificationLabel(_ qualification: ResearchQualification?) -> String {
        guard let qualification else { return "Qualification unknown" }
        if let qualified = qualification.qualified { return qualified ? "Qualified" : "Not qualified" }
        guard let status = qualification.status, !status.isEmpty else { return "Qualification unknown" }
        return PrismFormat.label(status)
    }

    private var runtimeControl: some View {
        ConsoleCard {
            HStack(spacing: 12) {
                ConsoleIcon(symbol: "power")
                VStack(alignment: .leading, spacing: 5) {
                    Text(runtime.isConnected ? PrismFormat.label(runtime.state?.runtime.status ?? "starting") : "Offline")
                        .font(.system(size: 12, weight: .medium))
                    Text("\(PrismFormat.label(runtime.state?.runtime.mode ?? "paper")) runtime")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                Button {
                    Task { await runtime.control(agentsAreRunning ? "pause" : "start_all") }
                } label: {
                    Label(agentsAreRunning ? "Stop" : "Start", systemImage: agentsAreRunning ? "pause" : "play")
                        .font(.system(size: 12, weight: .medium)).frame(minWidth: 82, minHeight: 36)
                }
                .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .capsule)
                .help(agentsAreRunning ? "Stop all agents" : "Start all agents")
                .accessibilityLabel(agentsAreRunning ? "Stop all agents" : "Start all agents")
                .disabled(!runtime.isConnected || runtime.isBusy)
            }
            if let issue = runtime.state?.runtime.lastError, !issue.isEmpty {
                Text(issue).font(.system(size: 11)).foregroundStyle(.orange).textSelection(.enabled)
            }
            if runtime.state?.portfolio.valuationComplete == false {
                Text("Valuation incomplete · fresh marks are missing.").font(.system(size: 11)).foregroundStyle(.orange)
            }
            if runtime.state?.portfolio.riskHalted == true {
                Text("Entries halted. Open positions may remain until observed exit prices are available.")
                    .font(.system(size: 11)).foregroundStyle(.orange)
            }
        }
    }
}
