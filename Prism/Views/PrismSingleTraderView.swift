import Charts
import SwiftUI

enum TradingAccountPresentation {
    static func utcDay(for date: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0) ?? .current
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    static func isCurrent(_ mode: TradingModeStatus, connected: Bool, now: Date) -> Bool {
        guard connected, mode.accountStatus == "current",
              let day = mode.accountDay, day == utcDay(for: now),
              let asOf = mode.accountAsOf, let recordedAt = PrismFormat.date(asOf),
              recordedAt <= now else { return false }
        return utcDay(for: recordedAt) == day
    }

    static func pnlTitle(_ mode: TradingModeStatus, current: Bool) -> String {
        guard mode.tradingPnl != nil else { return "Trading P&L · unavailable" }
        return current ? "Trading P&L · today" : "Trading P&L · recorded"
    }

    static func equityTitle(current: Bool) -> String {
        current ? "Account equity" : "Account equity · snapshot"
    }
}

/// Focused SOL workspace; exchange execution stays distinct from research simulation.
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
    private var solExecution: SOLExecutionProjection? { mode?.actualSolExecution }
    private var solPositions: [SOLExecutionPosition] { solExecution?.positions ?? [] }
    private var solTrades: [SOLExecutionFill] { solExecution?.fills ?? [] }
    private var trader: AgentStatus? {
        runtime.state?.agents.first { $0.roleId == "sol_trader" }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                quoteCard
                tradeReviewCard
                accountMetrics
                executionRiskCard
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
                    if let quote {
                        if runtime.isConnected {
                            TimelineView(.periodic(from: .now, by: 1)) { context in
                                let freshness = quote.freshness(at: context.date)
                                Text(quoteStatus(quote, freshness: freshness))
                                    .foregroundStyle(isStale(freshness) ? Color.orange : Color.secondary)
                            }
                        } else {
                            Text("Offline snapshot · Quote \(PrismFormat.time(quote.updatedAt))")
                        }
                    } else if let candle = candles.last {
                        Text("No live quote · Last candle \(PrismFormat.time(candle.date.ISO8601Format()))")
                    } else {
                        Text(runtime.state == nil && !runtime.isConnected ? "Runtime offline · Awaiting market data" : "Awaiting market data")
                    }
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

    @ViewBuilder private var executionRiskCard: some View {
        if let mode, mode.isSolPerpetual, let operations = runtime.state?.operations {
            let orderSection = operations.section(id: "orders")
            let blockedSection = operations.section(id: "blocked")
            let orders = orderSection?.items ?? []
            let blocked = blockedSection?.items ?? []
            let snapshot = runtime.isConnected ? "HOST RISK" : "OFFLINE SNAPSHOT · HOST RISK"
            ConsoleCard("Execution & risk", caption: "\(snapshot) · \(PrismFormat.label(operations.riskStatus ?? "unknown"))") {
                HStack(spacing: 8) {
                    Text("Risk control")
                        .font(.system(size: 11, weight: .medium))
                    Spacer(minLength: 4)
                    StatusPill(status: operations.riskStatus ?? "unknown")
                }
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Gross exposure")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    Spacer(minLength: 4)
                    Text(money(operations.grossExposure))
                        .font(.system(size: 11, weight: .medium)).monospacedDigit()
                }
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Modeled stop risk cap")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    Spacer(minLength: 4)
                    Text(PrismFormat.percent(mode.maxPositionRiskFraction))
                        .font(.system(size: 11, weight: .medium)).monospacedDigit()
                }
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Agent-selected leverage")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    Spacer(minLength: 4)
                    Text("\(leverage(mode.leverage)) · venue ceiling \(leverage(mode.venueMaxLeverage))")
                        .font(.system(size: 10, weight: .medium)).monospacedDigit()
                }

                if !blocked.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Blocked or deferred")
                            .font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                        ForEach(blocked.prefix(3)) { item in
                            HStack(alignment: .top, spacing: 7) {
                                Circle().fill(Color.orange).frame(width: 5, height: 5).padding(.top, 4)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.title).font(.system(size: 10, weight: .medium))
                                    Text(item.detail).font(.system(size: 9)).foregroundStyle(.secondary)
                                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        if blocked.count > 3 {
                            Text("+\(blocked.count - 3) more in Execution")
                                .font(.system(size: 9)).foregroundStyle(.tertiary)
                        }
                    }
                } else if blockedSection != nil {
                    Text("No blocked or deferred actions are reported.")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                } else {
                    Text("Blocked-action details unavailable.")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }

                VStack(alignment: .leading, spacing: 7) {
                    Text("Recent and unresolved orders")
                        .font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                    if let activeCount = operations.activeOrderCount {
                        Text("\(activeCount) unresolved\(operations.orderCount.map { " · \($0) total recorded" } ?? "")")
                            .font(.system(size: 9)).foregroundStyle(.secondary)
                    }
                    if orders.isEmpty {
                        Text(orderSection == nil ? "Order outcomes unavailable." : "No venue order outcomes recorded.")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                    } else {
                        ForEach(orders.prefix(3)) { order in
                            HStack(alignment: .top, spacing: 8) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(order.title).font(.system(size: 10, weight: .medium))
                                    Text(order.detail).font(.system(size: 9)).foregroundStyle(.secondary)
                                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                                }
                                Spacer(minLength: 4)
                                if let status = order.status { StatusPill(status: status) }
                            }
                        }
                        if orders.count > 3 || operations.ordersTruncated == true {
                            Text("Showing \(min(3, orders.count)) · \(orders.count) in Execution\(operations.ordersTruncated == true ? " · history limited" : "")")
                                .font(.system(size: 9)).foregroundStyle(.tertiary)
                        }
                    }
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
            TimelineView(.periodic(from: .now, by: 5)) { context in
                let current = TradingAccountPresentation.isCurrent(mode, connected: runtime.isConnected, now: context.date)
                ConsoleMetricGrid {
                    ConsoleMetric(title: TradingAccountPresentation.pnlTitle(mode, current: current),
                        value: money(mode.tradingPnl),
                        detail: tradingPnlDetail(mode, current: current, now: context.date))
                    ConsoleMetric(title: TradingAccountPresentation.equityTitle(current: current),
                        value: money(mode.accountEquity),
                        detail: accountSnapshotDetail(mode, current: current, now: context.date))
                    ConsoleMetric(title: "API model cost", value: money(mode.apiSpentUsd), detail: "Reserved \(money(mode.apiReservedUsd))")
                }
            }
        }
    }

    private func tradingPnlDetail(_ mode: TradingModeStatus, current: Bool, now: Date) -> String {
        if mode.tradingPnl != nil {
            let percent = PrismFormat.percent(mode.tradingReturn)
            let day = mode.accountDay ?? "unknown"
            if current {
                return "\(percent) · since first day snapshot · after venue costs · UTC \(day)"
            }
            let sourceTime = mode.accountAsOf.map { PrismFormat.time($0) } ?? "time unavailable"
            let source = !runtime.isConnected ? "offline snapshot" :
                day != TradingAccountPresentation.utcDay(for: now) ? "prior UTC day snapshot" : "recorded snapshot"
            return "\(percent) · recorded after venue costs · \(source) · UTC day \(day) · as of \(sourceTime)"
        }
        if let day = mode.accountDay {
            return "Last account day \(day); today's venue-cost P&L is not available."
        }
        return mode.accountStatus == "stale" || !runtime.isConnected
            ? "Account snapshot is stale; today's venue-cost P&L is unavailable."
            : "No verified current-day account P&L is available."
    }

    private func accountSnapshotDetail(_ mode: TradingModeStatus, current: Bool, now: Date) -> String {
        let environment = "Bybit \(mode.actualSolExecution?.environment?.uppercased() ?? runtime.state?.runtime.mode.uppercased() ?? "")"
            .trimmingCharacters(in: .whitespaces)
        guard let asOf = mode.accountAsOf else { return "\(environment) · account snapshot unavailable" }
        let recorded = "\(environment) · recorded \(PrismFormat.time(asOf))"
        let day = mode.accountDay.map { " · UTC day \($0)" } ?? ""
        guard !current else { return recorded + day }
        var reasons: [String] = []
        if !runtime.isConnected { reasons.append("offline snapshot") }
        if let accountDay = mode.accountDay,
           accountDay != TradingAccountPresentation.utcDay(for: now) {
            reasons.append("prior UTC day")
        }
        switch mode.accountStatus {
        case "stale": reasons.append("stale beyond the configured age")
        case "unverified": reasons.append("account binding unverified")
        case "current": break
        default: reasons.append("freshness unavailable")
        }
        return recorded + day + (reasons.isEmpty ? " · recorded snapshot" : " · " + reasons.joined(separator: " · "))
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
        ConsoleCard("Bybit SOL linear position", caption: executionCaption) {
            if solExecution?.positionsStatus == "unavailable" || solExecution == nil {
                Text("Actual Bybit position unavailable. An empty account cannot be confirmed.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            } else if solPositions.isEmpty {
                if solExecution?.confirmedFlat == true {
                    Text("No open SOL linear position · confirmed at \(PrismFormat.time(solExecution?.positionsAsOf))")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                } else {
                    Text("SOL position snapshot is stale or unreconciled; flatness is unconfirmed.\(asOfSuffix)")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
            } else {
                ForEach(solPositions) { position in
                    HStack(spacing: 8) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("\(PrismFormat.label(position.side)) · \(position.quantity.formatted()) SOL")
                                .font(.system(size: 11, weight: .medium))
                            Text("Entry \(PrismFormat.money(position.entryPrice)) · Mark \(PrismFormat.money(position.markPrice))")
                                .font(.system(size: 9)).foregroundStyle(.secondary)
                            Text(positionOwnership(position.ownership) + (position.leverage.map { " · \(leverage($0))" } ?? ""))
                                .font(.system(size: 9)).foregroundStyle(.tertiary)
                        }
                        Spacer(minLength: 4)
                        if let stopLoss = position.stopLoss {
                            Text("Stop \(PrismFormat.money(stopLoss))")
                                .font(.system(size: 9, weight: .medium)).monospacedDigit().foregroundStyle(.secondary)
                        }
                    }
                }
                if solExecution?.positionsTruncated == true {
                    Text("Additional SOL account exposure is omitted from this bounded view.")
                        .font(.system(size: 9)).foregroundStyle(.tertiary)
                }
                if solExecution?.positionsStatus != "confirmed" {
                    Text("Last matching environment snapshot\(asOfSuffix); reconciliation is not current.")
                        .font(.system(size: 9)).foregroundStyle(.orange)
                }
            }
        }.frame(maxWidth: .infinity)
    }

    private var tradesCard: some View {
        ConsoleCard("Actual managed SOL fills", caption: fillCaption) {
            if solExecution?.fillsAvailable != true {
                Text("Managed fill history unavailable for this environment.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            } else if solTrades.isEmpty {
                Text("No managed SOL fills recorded.").font(.system(size: 11)).foregroundStyle(.secondary)
            } else {
                ForEach(solTrades) { trade in
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("\(PrismFormat.label(trade.side)) · \(trade.quantity.formatted()) SOL")
                                .font(.system(size: 11, weight: .medium))
                            Text("\(PrismFormat.number(trade.price)) USDT · \(PrismFormat.time(trade.timestamp))")
                                .font(.system(size: 9)).foregroundStyle(.secondary)
                            if let fee = trade.fee {
                                Text("Fee \(fee.formatted()) \(trade.feeCurrency.flatMap { $0.isEmpty ? nil : $0 } ?? "currency unknown")")
                                    .font(.system(size: 9)).foregroundStyle(.tertiary)
                            }
                        }
                        Spacer(minLength: 4)
                        if let pnl = trade.realizedPnl {
                            VStack(alignment: .trailing, spacing: 2) {
                                Text("\(PrismFormat.number(pnl)) USDT").font(.system(size: 10)).monospacedDigit()
                                    .foregroundStyle(pnl < 0 ? Color.orange : Color.mint)
                                Text("Gross realized").font(.system(size: 9)).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if solExecution?.fillsTruncated == true {
                    Text("Showing the latest 5 managed fills.").font(.system(size: 9)).foregroundStyle(.tertiary)
                }
            }
        }.frame(maxWidth: .infinity)
    }

    private var executionCaption: String {
        guard let solExecution else { return "UNAVAILABLE" }
        let environment = "BYBIT \(solExecution.environment?.uppercased() ?? "")".trimmingCharacters(in: .whitespaces)
        let count = solExecution.positions?.count
        let state = solExecution.positionsStatus == "confirmed" ? "CONFIRMED" :
            solExecution.positionsStatus == "stale" ? "STALE" : "UNAVAILABLE"
        return "\(environment) · \(count.map(String.init) ?? "—") OPEN · \(state)"
    }

    private var fillCaption: String {
        guard let solExecution else { return "UNAVAILABLE" }
        let environment = "BYBIT \(solExecution.environment?.uppercased() ?? "")".trimmingCharacters(in: .whitespaces)
        return "\(environment) · \(solExecution.fillsAvailable == true ? "\(solTrades.count) RECENT" : "UNAVAILABLE")"
    }

    private var asOfSuffix: String {
        guard let asOf = solExecution?.positionsAsOf else { return "" }
        return " · as of \(PrismFormat.time(asOf))"
    }

    private func positionOwnership(_ ownership: String?) -> String {
        switch ownership {
        case "managed": "SOL trader exposure"
        case "other_strategy": "Other strategy exposure"
        default: "Unattributed account exposure"
        }
    }

    private func money(_ value: Double?) -> String { value.map { PrismFormat.money($0) } ?? "—" }

    private func leverage(_ value: Double?) -> String {
        value.map { String(format: "%.2fx", $0) } ?? "—"
    }

    private func quoteStatus(_ quote: MarketQuote, freshness: MarketQuoteFreshness) -> String {
        switch freshness {
        case let .current(age):
            return "Live quote · \(age.formatted(.number.precision(.fractionLength(0))))s ago"
        case let .stale(age):
            let elapsed = age >= 3_600 ? "\(Int(age / 3_600))h" : age >= 60 ? "\(Int(age / 60))m" : "\(Int(age))s"
            return "Stale quote · \(elapsed) old · \(PrismFormat.time(quote.updatedAt))"
        case .clockSkew:
            return "Quote timestamp is ahead of device clock"
        case .unavailable:
            return "Quote time unavailable"
        }
    }

    private func isStale(_ freshness: MarketQuoteFreshness) -> Bool {
        switch freshness {
        case .stale, .clockSkew, .unavailable: true
        case .current: false
        }
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
        let first = candles.first!.date
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
