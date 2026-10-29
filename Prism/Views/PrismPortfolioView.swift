import SwiftUI

/// Embedded in Execution. Every figure and fill here belongs to the paper ledger.
struct PrismPortfolioView: View {
    @ObservedObject var runtime: RuntimeClient
    var body: some View {
        VStack(alignment: .leading, spacing: 32) {
            VStack(alignment: .leading, spacing: 10) {
                Text("PAPER · SIMULATED BALANCE").font(.system(size: 9, weight: .medium, design: .monospaced)).tracking(1.5).foregroundStyle(.secondary)
                Text(runtime.state.map { PrismFormat.money($0.portfolio.equity) } ?? "—")
                    .font(.system(size: 30, weight: .regular, design: .rounded)).monospacedDigit()
                HStack(spacing: 22) {
                    Text("Realized \(runtime.state.map { PrismFormat.money($0.portfolio.realizedPnl) } ?? "—")")
                    Text("Unrealized \(runtime.state.map { PrismFormat.money($0.portfolio.unrealizedPnl) } ?? "—")")
                }.font(.system(size: 11)).foregroundStyle(.secondary)
            }.padding(.vertical, 10)
            VStack(alignment: .leading, spacing: 18) {
                Text("Positions").font(.system(size: 13, weight: .medium))
                if let positions = runtime.state?.portfolio.positions, !positions.isEmpty {
                    ForEach(positions) { position in
                        HStack {
                            Text(position.symbol)
                            Text("\(position.quantity.formatted()) · \(PrismFormat.label(position.side))").foregroundStyle(.secondary)
                            Spacer()
                            Text(PrismFormat.money(position.unrealizedPnl)).monospacedDigit()
                        }.font(.system(size: 12))
                    }
                } else {
                    Text("No open paper positions.").font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            VStack(alignment: .leading, spacing: 18) {
                Text("Paper fills").font(.system(size: 13, weight: .medium))
                if let trades = runtime.state?.portfolio.trades, !trades.isEmpty {
                    ForEach(trades) { trade in
                        HStack {
                            Text("\(PrismFormat.label(trade.side)) \(trade.symbol)")
                            Text("\(trade.quantity.formatted()) at \(PrismFormat.money(trade.price))").foregroundStyle(.secondary)
                            Spacer()
                            Text(PrismFormat.time(trade.timestamp)).foregroundStyle(.tertiary)
                        }.font(.system(size: 11))
                    }
                } else {
                    Text("No simulated fills.").font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            DisclosureGroup("Markets") {
                VStack(spacing: 18) {
                    if let markets = runtime.state?.markets, !markets.isEmpty {
                        ForEach(markets) { market in
                            HStack {
                                Text(market.symbol)
                                Spacer()
                                Text(PrismFormat.money(market.price)).monospacedDigit()
                                Text(PrismFormat.percent(market.change24h))
                                    .foregroundStyle(market.change24h < 0 ? Color.orange : .mint).frame(width: 72, alignment: .trailing)
                            }.font(.system(size: 12))
                        }
                    } else {
                        Text("Waiting for market data.").font(.system(size: 12)).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.padding(.vertical, 18).padding(.leading, 18)
            }.font(.system(size: 13))
        }.padding(.horizontal, 4)
    }
}
