import Charts
import SwiftUI

struct PrismOverviewView: View {
    @ObservedObject var runtime: RuntimeClient
    let availableHeight: CGFloat

    private var agentsAreRunning: Bool {
        guard let status = runtime.state?.runtime.status else { return false }
        return status != "paused"
    }

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 20)
            PrismPnlChart(portfolio: runtime.state?.portfolio,
                          samples: runtime.pnlSamples,
                          chartHeight: max(120, min(250, availableHeight * 0.40)))
                .frame(maxWidth: 720)
                .padding(.horizontal, 16)
            Spacer(minLength: 28)
            runtimeControl
                .padding(.bottom, 16)
        }
        .frame(maxWidth: .infinity).frame(minHeight: max(300, availableHeight))
    }

    private var runtimeControl: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Circle().fill(runtime.isConnected ? Color.mint : .secondary).frame(width: 4, height: 4)
                Text(runtime.isConnected ? "\(PrismFormat.label(runtime.state?.runtime.status ?? "starting")) · Paper" : "Offline")
            }
            .font(.system(size: 11)).foregroundStyle(.secondary).padding(.top, 8)
            Button {
                Task { await runtime.control(agentsAreRunning ? "pause" : "start_all") }
            } label: {
                Text(agentsAreRunning ? "Stop" : "Start")
                    .font(.system(size: 13, weight: .medium))
                    .frame(minWidth: 116, minHeight: 42)
            }
            .buttonStyle(.plain)
            .glassEffect(.clear.interactive(), in: .capsule)
            .help(agentsAreRunning ? "Stop all agents" : "Start all agents")
            .accessibilityLabel(agentsAreRunning ? "Stop all agents" : "Start all agents")
            .disabled(!runtime.isConnected || runtime.isBusy)
            .padding(.top, 20)
            if let issue = runtime.state?.runtime.lastError, !issue.isEmpty {
                Text(issue).font(.system(size: 11)).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center).frame(maxWidth: 360).padding(.top, 20)
            }
        }
    }
}

/// Every vertex is an observed ledger value. The chart never smooths, buckets,
/// or invents historical returns, including when migrating an older runtime.
struct PrismPnlChart: View {
    let portfolio: PaperPortfolio?
    let samples: [PaperPnlSample]
    let chartHeight: CGFloat
    @State private var hoveredDate: Date?

    var body: some View {
        let selected = hoveredDate.flatMap { nearestSample(to: $0, in: samples) }
        let pnl = selected?.pnl ?? portfolio?.totalPnl
        let returnValue = selected.flatMap { sample in
            portfolio?.initialEquity.flatMap { initial in
                initial.isFinite && initial > 0 ? sample.pnl / initial : nil
            }
        } ?? portfolio?.totalReturn

        VStack(spacing: 30) {
            VStack(spacing: 8) {
                Text(pnl.map { signedMoney($0) } ?? "—")
                    .font(.system(size: 44, weight: .light, design: .rounded))
                    .monospacedDigit()
                    .minimumScaleFactor(0.5)
                    .lineLimit(1)
                    .accessibilityLabel(selected == nil ? "All-time total paper profit and loss" : "Selected total paper profit and loss")
                    .accessibilityValue(pnl.map { signedMoney($0) } ?? "Unavailable")
                Text(returnValue.map { signedPercent($0) } ?? "—")
                    .font(.system(size: 13, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(tint(for: pnl))
                    .accessibilityLabel("Return on initial paper capital")
                    .accessibilityValue(returnValue.map { signedPercent($0) } ?? "Unavailable")
            }
            .help(summaryHelp(selected: selected, samples: samples))

            Chart {
                ForEach(samples) { sample in
                    LineMark(x: .value("Recorded time", sample.date),
                             y: .value("Total paper P&L", sample.pnl))
                        .interpolationMethod(.linear)
                        .lineStyle(StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
                        .foregroundStyle(Color.primary.opacity(0.8))
                }
                if let point = selected ?? (samples.count == 1 ? samples.first : nil) {
                    PointMark(x: .value("Recorded time", point.date),
                              y: .value("Total paper P&L", point.pnl))
                        .symbolSize(20)
                        .foregroundStyle(Color.primary)
                }
            }
            .chartXAxis(.hidden)
            .chartYAxis(.hidden)
            .chartLegend(.hidden)
            .chartXScale(domain: timeDomain(samples))
            .chartYScale(domain: pnlDomain(samples))
            .chartPlotStyle { $0.background(.clear) }
            .chartOverlay { proxy in
                GeometryReader { geometry in
                    Color.clear
                        .contentShape(Rectangle())
                        .onContinuousHover { phase in
                            switch phase {
                            case .active(let location):
                                guard let plotFrame = proxy.plotFrame else { return }
                                let frame = geometry[plotFrame]
                                hoveredDate = frame.contains(location)
                                    ? proxy.value(atX: location.x - frame.minX, as: Date.self) : nil
                            case .ended:
                                hoveredDate = nil
                            }
                        }
                }
            }
            .accessibilityLabel("All-time total paper profit and loss history")
            .help(summaryHelp(selected: selected, samples: samples))
            .frame(height: chartHeight)
        }
    }

    private func nearestSample(to date: Date, in samples: [PaperPnlSample]) -> PaperPnlSample? {
        // Upper-bound search retains the final ledger observation at equal times.
        var lower = 0
        var upper = samples.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if samples[middle].date <= date { lower = middle + 1 }
            else { upper = middle }
        }
        guard lower > 0 else { return samples.first }
        guard lower < samples.count else { return samples.last }
        let before = samples[lower - 1]
        let after = samples[lower]
        return date.timeIntervalSince(before.date) <= after.date.timeIntervalSince(date) ? before : after
    }

    private func timeDomain(_ samples: [PaperPnlSample]) -> ClosedRange<Date> {
        guard let first = samples.first?.date, let last = samples.last?.date else {
            return Date(timeIntervalSince1970: 0)...Date(timeIntervalSince1970: 1)
        }
        return first < last ? first...last : first.addingTimeInterval(-1)...last.addingTimeInterval(1)
    }

    private func pnlDomain(_ samples: [PaperPnlSample]) -> ClosedRange<Double> {
        // Include zero to keep small gains/losses in proportion; no visible baseline.
        let lower = min(0, samples.map(\.pnl).min() ?? 0)
        let upper = max(0, samples.map(\.pnl).max() ?? 0)
        let padding = upper > lower ? (upper - lower) * 0.08 : 1
        return (lower - padding)...(upper + padding)
    }

    private func signedMoney(_ value: Double) -> String {
        (value >= 0.005 ? "+" : "") + PrismFormat.money(abs(value) < 0.005 ? 0 : value)
    }

    private func signedPercent(_ value: Double) -> String {
        (value >= 0.00005 ? "+" : "") + PrismFormat.percent(abs(value) < 0.00005 ? 0 : value)
    }

    private func tint(for pnl: Double?) -> Color {
        guard let pnl, abs(pnl) >= 0.005 else { return .secondary }
        return pnl < 0 ? .orange : .mint
    }

    private func summaryHelp(selected: PaperPnlSample?, samples: [PaperPnlSample]) -> String {
        if let selected {
            return selected.date.formatted(date: .abbreviated, time: .standard)
                + " · " + selected.pnl.formatted(.number.precision(.fractionLength(2...8))) + " USD"
        }
        let explanation = "All-time paper P&L, including open positions and simulated trading costs."
        guard let first = samples.first else { return explanation + " No recorded history yet." }
        return explanation + " Recorded history begins "
            + first.date.formatted(date: .abbreviated, time: .shortened) + "."
    }
}
