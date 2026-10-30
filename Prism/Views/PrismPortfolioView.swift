import SwiftUI

/// Embedded in Execution. Every figure and fill here belongs to the paper ledger.
struct PrismPortfolioView: View {
    @ObservedObject var runtime: RuntimeClient

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ConsoleMetricGrid {
                ConsoleMetric(
                    title: "Paper equity",
                    value: runtime.state.map { PrismFormat.money($0.portfolio.equity) } ?? "—",
                    detail: "Simulated balance"
                )
                ConsoleMetric(
                    title: "Realized P&L",
                    value: runtime.state.map { PrismFormat.money($0.portfolio.realizedPnl) } ?? "—",
                    detail: "Paper ledger"
                )
                ConsoleMetric(
                    title: "Unrealized P&L",
                    value: runtime.state.map { PrismFormat.money($0.portfolio.unrealizedPnl) } ?? "—",
                    detail: "Open paper positions"
                )
            }

            if let portfolio = runtime.state?.portfolio {
                if let warnings = portfolio.warnings, !warnings.isEmpty {
                    ConsoleCard("Paper ledger notes") {
                        ForEach(warnings, id: \.self) { warning in
                            Label(warning, systemImage: "exclamationmark.circle")
                                .font(.system(size: 11)).foregroundStyle(.orange)
                        }
                    }
                }
                if let receivables = runtime.state?.portfolio.receivables, !receivables.isEmpty {
                    ConsoleCard("Income receivable") {
                        Text("\(PrismFormat.money(receivables.reduce(0) { $0 + $1.amount })) · Cash awaits an observed payment date")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }

                ConsoleCard("Positions", caption: "PAPER") {
                    if portfolio.positions.isEmpty {
                        Text("No open paper positions.").font(.system(size: 11)).foregroundStyle(.secondary)
                    } else {
                        ForEach(portfolio.positions) { position in
                            HStack(spacing: 8) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(position.symbol).font(.system(size: 12, weight: .medium))
                                    Text("\(position.quantity.formatted()) · \(PrismFormat.label(position.side))")
                                        .font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 4)
                                Text(PrismFormat.money(position.unrealizedPnl)).font(.system(size: 11)).monospacedDigit()
                            }.padding(.vertical, 3)
                        }
                    }
                }

                ConsoleCard("Paper fills", caption: "SIMULATED") {
                    if portfolio.trades.isEmpty {
                        Text("No simulated fills.").font(.system(size: 11)).foregroundStyle(.secondary)
                    } else {
                        ForEach(portfolio.trades) { trade in
                            HStack(alignment: .top, spacing: 8) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text("\(PrismFormat.label(trade.side)) \(trade.symbol)")
                                        .font(.system(size: 12, weight: .medium))
                                    Text("\(trade.quantity.formatted()) at \(PrismFormat.money(trade.price))")
                                        .font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 4)
                                Text(PrismFormat.time(trade.timestamp)).font(.system(size: 10))
                                    .foregroundStyle(.tertiary).multilineTextAlignment(.trailing)
                            }.padding(.vertical, 3)
                        }
                    }
                }

                ConsoleCard {
                    DisclosureGroup("Markets · \(runtime.state?.markets.count ?? 0)") {
                        VStack(spacing: 8) {
                            if let markets = runtime.state?.markets, !markets.isEmpty {
                                ForEach(markets) { market in
                                    HStack(spacing: 8) {
                                        Text(market.symbol).fontWeight(.medium)
                                        Spacer(minLength: 4)
                                        VStack(alignment: .trailing, spacing: 2) {
                                            Text(PrismFormat.money(market.price)).monospacedDigit()
                                            Text(PrismFormat.percent(market.change24h))
                                                .foregroundStyle(market.change24h.map { $0 < 0 ? Color.orange : .mint } ?? .secondary)
                                        }.font(.system(size: 10))
                                    }
                                }
                            } else {
                                Text("Waiting for market data.").font(.system(size: 11)).foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }.padding(.top, 10)
                    }.font(.system(size: 12, weight: .medium))
                }
            }
        }
    }
}
