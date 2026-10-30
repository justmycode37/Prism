import Charts
import SwiftUI

/// Focused SOL workspace; account metrics and the paper ledger stay distinct.
struct PrismSingleTraderView: View {
    @ObservedObject var runtime: RuntimeClient
    let availableHeight: CGFloat

    private var mode: TradingModeStatus? { runtime.state?.tradingMode }
    private var quote: MarketQuote? { runtime.state?.markets.first { $0.symbol == "SOLUSDT" } }
    private var candles: [SOLCandle] {
        guard let state = runtime.state else { return [] }
        return state.solChart?.displayCandles(for: state.runtime.mode) ?? []
    }
    private var price: Double? { quote?.price ?? candles.last?.close }
    private var solPositions: [PaperPosition] {
        (runtime.state?.portfolio.positions ?? []).filter { $0.symbol == "SOLUSDT" }
    }
    private var solTrades: [PaperTrade] {
        Array((runtime.state?.portfolio.trades ?? []).filter { $0.symbol == "SOLUSDT" }
            .sorted { $0.timestamp > $1.timestamp }.prefix(5))
    }
    private var trader: AgentStatus? {
        runtime.state?.agents.first { $0.roleId == "sol_trader" }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                quoteCard
                tradeReviewCard
                accountMetrics
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: 14) {
                        positionsCard
                        tradesCard
                    }
                    VStack(spacing: 14) { positionsCard; tradesCard }
                }
                if let trader {
                    ConsoleCard("SOL trader", caption: PrismFormat.label(trader.status)) {
                        Text(trader.summary.isEmpty ? trader.task : trader.summary)
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.bottom, 16)
        }
        .scrollIndicators(.hidden)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }

    private var quoteCard: some View {
        ConsoleCard("SOLUSDT", caption: "Perpetual · 1m candles") {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(price.map { PrismFormat.money($0) } ?? "Waiting for quote")
                    .font(.system(size: 30, weight: .medium, design: .rounded)).monospacedDigit()
                    .lineLimit(1).minimumScaleFactor(0.7)
                Spacer(minLength: 4)
                if let change = quote?.change24h {
                    Text("\(PrismFormat.percent(change)) · 24h")
                        .font(.system(size: 12, weight: .medium)).monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }
            quoteChart
                .frame(height: max(140, min(176, availableHeight * 0.48)))
                .accessibilityLabel("SOLUSDT one-minute candlestick chart")
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(quote.map(quoteStatus)
                         ?? candles.last.map { "Last candle · \(PrismFormat.time($0.date.ISO8601Format()))" }
                         ?? "Awaiting market data")
                    if let funding = quote?.fundingRate { Text("Funding \(PrismFormat.percent(funding))") }
                }
                .font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer(minLength: 8)
                if let mode, mode.isSolPerpetual {
                    HStack(spacing: 5) {
                        Circle().fill(.secondary).frame(width: 5, height: 5)
                        Text(runtime.isConnected ? (mode.enabled == true ? "Running" : "Paused") : "Offline")
                    }
                    .font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                    .padding(.horizontal, 8).padding(.vertical, 5)
                    .background(.primary.opacity(0.045), in: .capsule)
                }
            }
        }
    }

    @ViewBuilder private var quoteChart: some View {
        if !candles.isEmpty {
            SOLCandlestickChart(candles: candles)
        } else {
            ConsoleEmptyState(symbol: "chart.bar.xaxis", title: "Waiting for SOL candles",
                              message: "One-minute candles appear when market history is available.")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder private var accountMetrics: some View {
        if let mode, mode.isSolPerpetual {
            ConsoleMetricGrid {
                ConsoleMetric(title: "Trading P&L · today", value: money(mode.tradingPnl), detail: "\(PrismFormat.percent(mode.tradingReturn)) · after venue costs")
                ConsoleMetric(title: "Account equity", value: money(mode.accountEquity), detail: "SOL perpetual account")
                ConsoleMetric(title: "API model cost", value: money(mode.apiSpentUsd), detail: "Reserved \(money(mode.apiReservedUsd))")
            }
        }
    }

    @ViewBuilder private var tradeReviewCard: some View {
        if let mode, mode.isSolPerpetual, mode.tradeReviewRequired == true {
            ConsoleCard("Trade reviewer", caption: "Luna · \(mode.reviewerReasoningEffort ?? "max")") {
                VStack(alignment: .leading, spacing: 6) {
                    if let review = mode.latestTradeReview {
                        Text("Last review · \(PrismFormat.label(review.status))")
                            .font(.system(size: 11, weight: .medium))
                        Text(review.rationale ?? review.reason ?? "Review result recorded.")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text("Sol proposes a trade. Luna must approve it before Prism sends the order.")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private var positionsCard: some View {
        ConsoleCard("Paper SOL positions", caption: runtime.state == nil ? "OFFLINE" : "\(solPositions.count) OPEN") {
            if solPositions.isEmpty {
                Text(runtime.state == nil ? "Paper portfolio unavailable." : "No open SOL positions.").font(.system(size: 11)).foregroundStyle(.secondary)
            } else {
                ForEach(solPositions) { position in
                    HStack(spacing: 8) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("\(PrismFormat.label(position.side)) · \(position.quantity.formatted()) SOL")
                                .font(.system(size: 11, weight: .medium))
                            Text("Entry \(PrismFormat.money(position.entryPrice)) · Mark \(PrismFormat.money(position.markPrice))")
                                .font(.system(size: 9)).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 4)
                        Text(PrismFormat.money(position.unrealizedPnl))
                            .font(.system(size: 10, weight: .medium)).monospacedDigit()
                            .foregroundStyle(position.unrealizedPnl < 0 ? Color.orange : Color.mint)
                    }
                }
            }
        }.frame(maxWidth: .infinity)
    }

    private var tradesCard: some View {
        ConsoleCard("Paper SOL trades", caption: runtime.state == nil ? "OFFLINE" : "\(solTrades.count) LATEST") {
            if solTrades.isEmpty {
                Text(runtime.state == nil ? "Paper ledger unavailable." : "No SOL trades recorded.").font(.system(size: 11)).foregroundStyle(.secondary)
            } else {
                ForEach(solTrades) { trade in
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("\(PrismFormat.label(trade.side)) · \(trade.quantity.formatted()) SOL")
                                .font(.system(size: 11, weight: .medium))
                            Text("\(PrismFormat.money(trade.price)) · \(PrismFormat.time(trade.timestamp))")
                                .font(.system(size: 9)).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 4)
                        if let pnl = trade.realizedPnl {
                            Text(PrismFormat.money(pnl)).font(.system(size: 10)).monospacedDigit()
                                .foregroundStyle(pnl < 0 ? Color.orange : Color.mint)
                        }
                    }
                }
            }
        }.frame(maxWidth: .infinity)
    }

    private func money(_ value: Double?) -> String { value.map { PrismFormat.money($0) } ?? "—" }

    private func quoteStatus(_ quote: MarketQuote) -> String {
        guard let date = PrismFormat.date(quote.updatedAt) else { return "Quote time unavailable" }
        let age = Date.now.timeIntervalSince(date)
        if age > 15 || age < -5 {
            return "Last quote · \(date.formatted(date: .abbreviated, time: .shortened))"
        }
        return "Quote · \(PrismFormat.time(quote.updatedAt))"
    }
}

/// Keeps the trader control visible even when its chart scrolls in a small window.
struct PrismTraderControl: View {
    @ObservedObject var runtime: RuntimeClient
    @Environment(\.colorScheme) private var colorScheme

    private var mode: TradingModeStatus? { runtime.state?.tradingMode }
    private var canControl: Bool {
        // Starting observations is separate from permission to submit orders.
        runtime.isConnected && !runtime.isBusy && mode?.isSolPerpetual == true
    }

    var body: some View {
        if let mode, mode.isSolPerpetual {
            Button {
                Task { await runtime.control(mode.enabled == true ? "pause" : "start_all") }
            } label: {
                Label(mode.enabled == true ? "Pause trader" : "Start trader",
                      systemImage: mode.enabled == true ? "pause.fill" : "play.fill")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(colorScheme == .dark ? .white.opacity(0.92) : .black.opacity(0.84))
                    .padding(.horizontal, 12).frame(minHeight: 32)
            }
            .buttonStyle(.plain).glassEffect(.clear.interactive(), in: .capsule)
            .disabled(!canControl)
            .help(mode.enabled == true ? "Pause the SOL trader" : "Start SOL mode; orders still require execution approval")
            .accessibilityIdentifier("sol-trader-control")
        }
    }
}

/// Chart marks retain native axis and accessibility support; frosted candle
/// bodies are drawn in plot coordinates so their highlights follow real OHLC.
private struct SOLCandlestickChart: View {
    let candles: [SOLCandle]
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    private var domain: ClosedRange<Double> {
        let low = candles.map(\.low).min() ?? 0
        let high = candles.map(\.high).max() ?? 1
        let center = low + (high - low) / 2
        // Leave room around the market rather than stretching each small move
        // to fill the plot. Candle values and the chart's frame stay unchanged.
        let halfSpan = max((high - low) * 2, center * 0.025, 0.05)
        return max(0, center - halfSpan)...(center + halfSpan)
    }

    private var timeDomain: ClosedRange<Date> {
        let last = candles.last!.date
        let first = min(candles.first!.date, last.addingTimeInterval(-119 * 60))
        return first.addingTimeInterval(-30)...last.addingTimeInterval(90)
    }

    private var timeTicks: [Date] {
        let start = candles.first!.date.timeIntervalSince1970
        let end = candles.last!.date.timeIntervalSince1970 + 60
        let span = end - start
        let step = [60.0, 300, 600, 900, 1800, 3600].first { $0 >= span / 5 } ?? span / 4
        let margin = min(60, span / 8)
        let first = ceil((start + margin) / step) * step
        return stride(from: first, to: end - margin, by: step).prefix(5).map { Date(timeIntervalSince1970: $0) }
    }

    var body: some View {
        Chart(candles) { candle in
            RuleMark(x: .value("Time", candle.centerDate),
                     yStart: .value("Low", candle.low), yEnd: .value("High", candle.high))
                .foregroundStyle(candleTone.opacity(0.6))
                .lineStyle(StrokeStyle(lineWidth: 0.55, lineCap: .round))
                .accessibilityHidden(true)
            RectangleMark(x: .value("Time", candle.centerDate),
                          yStart: .value("Open", candle.open), yEnd: .value("Close", candle.close), width: .fixed(5))
                .foregroundStyle(.clear)
                .accessibilityLabel(candle.date.formatted(date: .abbreviated, time: .shortened))
                .accessibilityValue("Open \(PrismFormat.number(candle.open)), high \(PrismFormat.number(candle.high)), low \(PrismFormat.number(candle.low)), close \(PrismFormat.number(candle.close)); \(candle.closed ? "closed" : "forming")")
        }
        .chartXScale(domain: timeDomain)
        .chartYScale(domain: domain)
        .chartXAxis {
            AxisMarks(values: timeTicks) { _ in
                AxisValueLabel(format: .dateTime.hour().minute())
                    .font(.system(size: 9)).foregroundStyle(.secondary)
            }
        }
        .chartYAxis {
            AxisMarks(position: .trailing, values: .automatic(desiredCount: 4)) { _ in
                AxisGridLine().foregroundStyle(.primary.opacity(0.065))
                AxisValueLabel().font(.system(size: 9)).foregroundStyle(.secondary)
            }
        }
        .chartOverlay { proxy in
            GeometryReader { geometry in
                if let plotFrame = proxy.plotFrame {
                    let plot = geometry[plotFrame]
                    ZStack(alignment: .topLeading) {
                        ForEach(candles) { candle in
                            if let x = proxy.position(forX: candle.centerDate),
                               let openY = proxy.position(forY: candle.open),
                               let closeY = proxy.position(forY: candle.close) {
                                let nextX = proxy.position(forX: candle.centerDate.addingTimeInterval(60)) ?? (x + 10)
                                let width = max(1, min(4, abs(nextX - x) * 0.45))
                                candleBody(candle)
                                    .frame(width: width, height: max(0.9, abs(closeY - openY)))
                                    .position(x: x, y: (openY + closeY) / 2)
                            }
                        }
                    }
                    .frame(width: plot.width, height: plot.height, alignment: .topLeading)
                    .clipped()
                    .offset(x: plot.minX, y: plot.minY)
                }
            }
            .allowsHitTesting(false).accessibilityHidden(true)
        }
        .padding(.top, 12).padding(.leading, 12).padding(.trailing, 8).padding(.bottom, 8)
        .background {
            let shape = RoundedRectangle(cornerRadius: 16, style: .continuous)
            if reduceTransparency {
                shape.fill(colorScheme == .dark ? Color(white: 0.13) : Color(white: 0.95))
            } else {
                shape.fill(.ultraThinMaterial).opacity(0.38)
                shape.fill(LinearGradient(colors: [.white.opacity(0.08), .clear, .white.opacity(0.025)],
                                          startPoint: .topLeading, endPoint: .bottomTrailing))
            }
            shape.strokeBorder(.white.opacity(colorScheme == .dark ? 0.12 : 0.3), lineWidth: 0.6)
        }
    }

    private var candleTone: Color {
        colorScheme == .dark ? Color(white: 0.88) : Color(white: 0.24)
    }

    private func candleBody(_ candle: SOLCandle) -> some View {
        let tone = candleTone
        let shape = RoundedRectangle(cornerRadius: 0.7, style: .continuous)
        let fill = candle.isRising ? 0.08 : 0.5
        return shape
            .fill(reduceTransparency
                  ? AnyShapeStyle(colorScheme == .dark ? Color(white: candle.isRising ? 0.22 : 0.72) : Color(white: candle.isRising ? 0.94 : 0.32))
                  : AnyShapeStyle(.ultraThinMaterial))
            .overlay {
                shape.fill(LinearGradient(
                    colors: [tone.opacity(fill + 0.1), tone.opacity(fill * 0.4), tone.opacity(fill)],
                    startPoint: .topLeading, endPoint: .bottomTrailing))
            }
            .overlay {
                shape.strokeBorder(LinearGradient(colors: [.white.opacity(0.55), tone.opacity(0.7)],
                                                   startPoint: .topLeading, endPoint: .bottomTrailing),
                                   lineWidth: contrast == .increased ? 1 : 0.45)
            }
            .opacity(candle.closed ? 1 : 0.72)
    }
}
