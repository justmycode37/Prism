import SwiftUI

struct PrismResearchView: View {
    @ObservedObject var runtime: RuntimeClient
    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            sectorCoverage
            if let markets = runtime.state?.markets, !markets.isEmpty {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Equity observations").font(.system(size: 13, weight: .medium))
                    ForEach(markets) { quote in
                        HStack(spacing: 16) {
                            Text(quote.symbol).font(.system(size: 12, weight: .medium)).frame(width: 56, alignment: .leading)
                            Text(PrismFormat.money(quote.price)).monospacedDigit()
                            Spacer()
                            Text(PrismFormat.percent(quote.change24h)).monospacedDigit()
                            Text("As of \(PrismFormat.time(quote.updatedAt))")
                                .foregroundStyle(.secondary).frame(width: 112, alignment: .trailing)
                        }.font(.system(size: 11))
                    }
                    Text("Daily change · public market observations · paper trading")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }
            VStack(alignment: .leading, spacing: 18) {
                Text("Hypotheses").font(.system(size: 13, weight: .medium))
                if let strategies = runtime.state?.strategies, !strategies.isEmpty {
                    ForEach(strategies) { strategy in
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 12) {
                                Text(strategy.family == "python" ? "Authored Python" : PrismFormat.label(strategy.family))
                                    .font(.system(size: 11)).foregroundStyle(.secondary)
                                if let sourceSha256 = strategy.spec?.sourceSha256 {
                                    Text("Source version \(sourceSha256.prefix(12))")
                                        .font(.system(size: 10)).monospaced().foregroundStyle(.secondary)
                                }
                                if let parameters = strategy.spec?.parameters, !parameters.isEmpty {
                                    HStack(spacing: 22) {
                                        ForEach(parameters.keys.sorted(), id: \.self) { key in
                                            VStack(alignment: .leading, spacing: 5) {
                                                Text(PrismFormat.label(key)).foregroundStyle(.secondary)
                                                Text(parameters[key, default: 0].formatted(.number.precision(.fractionLength(0...4)))).monospacedDigit()
                                            }.font(.system(size: 10))
                                        }
                                    }
                                }
                                HStack {
                                    if let agentId = strategy.agentId,
                                       let agent = runtime.state?.agents.first(where: { $0.id == agentId }) {
                                        Text(agent.name)
                                    }
                                    Text(PrismFormat.time(strategy.createdAt))
                                }.font(.system(size: 10)).foregroundStyle(.tertiary)
                            }.padding(.vertical, 12).padding(.leading, 18)
                        } label: {
                            HStack {
                                Text(strategy.name).font(.system(size: 13))
                                Spacer()
                                Text(strategy.symbol).font(.system(size: 11)).foregroundStyle(.secondary)
                            }.padding(.vertical, 5)
                        }
                    }
                } else {
                    Text("No hypotheses registered yet.").font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
        }.padding(.horizontal, 4)
    }

    private var sectorCoverage: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("AI infrastructure").font(.system(size: 13, weight: .medium))
                Spacer()
                Text(PrismFormat.label(runtime.state?.cascade?.sector?.discoveryStatus ?? "pending"))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Text("Chips, memory, networking, cloud compute, data centers, power and cooling")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            if let collection = runtime.state?.cascade?.collection {
                Text("\(collection.collectorCount) collectors · \(collection.evidenceCount) evidence records · \(collection.sourceCount) sources")
                    .font(.system(size: 11)).monospacedDigit().foregroundStyle(.secondary)
            }
            if let sector = runtime.state?.cascade?.sector, !sector.companies.isEmpty {
                DisclosureGroup("Covered companies (\(sector.companies.count))") {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 180), alignment: .leading)], alignment: .leading, spacing: 16) {
                        ForEach(sector.companies) { company in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(company.symbol).font(.system(size: 12, weight: .medium))
                                Text(company.name).lineLimit(1)
                                if !company.themes.isEmpty {
                                    Text(company.themes.map(PrismFormat.label).joined(separator: ", ")).lineLimit(2)
                                }
                            }
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                            .help(company.discoveryReason)
                        }
                    }.padding(.top, 12)
                }.font(.system(size: 12))
            } else {
                Text("Discovery builds the company universe when research starts.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }
}
