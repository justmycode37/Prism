import SwiftUI

struct PrismResearchView: View {
    @ObservedObject var runtime: RuntimeClient

    private var strategies: [StrategyResult] { runtime.state?.strategies ?? [] }
    private var research: ResearchStatus? { runtime.state?.research }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            ConsoleMetricGrid {
                ConsoleMetric(title: "Hypotheses", value: runtime.state.map { String($0.strategies.count) } ?? "—", detail: "Candidate strategies")
                ConsoleMetric(title: "Evidence", value: runtime.state?.cascade?.collection.map { String($0.evidenceCount) } ?? "—", detail: "Collected records")
                ConsoleMetric(title: "Sources", value: runtime.state?.cascade?.collection.map { String($0.sourceCount) } ?? "—", detail: "Research coverage")
            }

            ConsoleCard("Hypotheses", caption: "Candidate strategies") {
                if strategies.isEmpty {
                    Text("No hypotheses registered yet.").font(.system(size: 12)).foregroundStyle(.secondary)
                } else {
                    ForEach(strategies) { strategy in
                        DisclosureGroup {
                            hypothesisDetails(strategy)
                        } label: {
                            HStack(spacing: 10) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(strategy.name).font(.system(size: 13, weight: .medium))
                                    Text("\(strategy.symbol) · \(PrismFormat.label(strategy.family))")
                                        .font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 6)
                                StatusPill(status: strategy.status)
                            }.padding(.vertical, 6)
                        }
                        if strategy.id != strategies.last?.id { Divider().opacity(0.45) }
                    }
                }
            }

            ConsoleCard("Research universe", caption: "Sources & coverage") {
                universeSummary
                DisclosureGroup("Market observations (\(runtime.state?.markets.count ?? 0))") {
                    if let markets = runtime.state?.markets, !markets.isEmpty {
                        VStack(spacing: 0) {
                            ForEach(markets) { quote in
                                HStack(spacing: 8) {
                                    Text(quote.symbol).fontWeight(.medium).frame(width: 52, alignment: .leading)
                                    Text(PrismFormat.money(quote.price)).monospacedDigit()
                                    Spacer(minLength: 2)
                                    Text(PrismFormat.percent(quote.change24h)).monospacedDigit()
                                    Text(PrismFormat.time(quote.updatedAt)).foregroundStyle(.secondary)
                                }.font(.system(size: 10)).padding(.vertical, 8)
                                if quote.id != markets.last?.id { Divider().opacity(0.4) }
                            }
                        }.padding(.top, 8)
                        Text("Daily change · public market observations · paper trading")
                            .font(.system(size: 10)).foregroundStyle(.secondary).padding(.top, 6)
                    } else {
                        Text("No public market observations are available in this snapshot.")
                            .font(.system(size: 11)).foregroundStyle(.secondary).padding(.top, 8)
                    }
                }
            }

            technicalDetails
        }
    }

    @ViewBuilder private var universeSummary: some View {
        let sector = runtime.state?.cascade?.sector
        let collection = runtime.state?.cascade?.collection
        HStack(alignment: .top, spacing: 10) {
            ConsoleIcon(symbol: "globe.americas")
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text("AI infrastructure").font(.system(size: 13, weight: .medium))
                    Spacer()
                    StatusPill(status: sector?.discoveryStatus ?? "pending")
                }
                Text("Chips, memory, networking, cloud compute, data centers, power and cooling")
                    .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if let collection {
                    Text("\(collection.activeCollectorCount ?? collection.collectorCount) active collectors · \(collection.evidenceCount) evidence records · \(collection.sourceCount) sources")
                        .font(.system(size: 10)).foregroundStyle(.secondary).monospacedDigit()
                }
            }
        }
        if let sector, !sector.companies.isEmpty {
            DisclosureGroup("Covered companies (\(sector.companies.count))") {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), alignment: .leading)], alignment: .leading, spacing: 12) {
                    ForEach(sector.companies) { company in
                        VStack(alignment: .leading, spacing: 4) {
                            Text("\(company.symbol) · \(company.name)").font(.system(size: 11, weight: .medium)).lineLimit(1)
                            if !company.themes.isEmpty { Text(company.themes.map(PrismFormat.label).joined(separator: ", ")).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2) }
                            Text(company.discoveryReason).font(.system(size: 10)).foregroundStyle(.tertiary).lineLimit(3).help(company.discoveryReason)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.padding(.top, 8)
            }.font(.system(size: 11))
        } else {
            Text("Discovery builds the company universe when research starts.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }

    private func hypothesisDetails(_ strategy: StrategyResult) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(strategy.spec?.kind == "python" ? "Authored Python" : PrismFormat.label(strategy.spec?.kind ?? strategy.family))
            if let hash = strategy.spec?.sourceSha256 { Text("Source version · \(hash)").textSelection(.enabled) }
            if let parameters = strategy.spec?.parameters, !parameters.isEmpty {
                ForEach(parameters.keys.sorted(), id: \.self) { key in
                    HStack { Text(PrismFormat.label(key)).foregroundStyle(.secondary); Spacer(); Text(parameters[key, default: 0].formatted(.number.precision(.fractionLength(0...4)))).monospacedDigit() }
                }
            }
            HStack {
                if let agentId = strategy.agentId, let agent = runtime.state?.agents.first(where: { $0.id == agentId }) { Text("Role · \(agent.name)") }
                Spacer()
                Text("Created · \(PrismFormat.time(strategy.createdAt))")
            }.foregroundStyle(.tertiary)
        }.font(.system(size: 10)).padding(.top, 8)
    }

    @ViewBuilder private var technicalDetails: some View {
        let failures = research?.failureReasons ?? [:]
        let gaps = research?.coverage?.gaps ?? []
        let costs = research?.operatingCosts
        if !failures.isEmpty || !gaps.isEmpty || costs != nil || research?.coverage?.survivorshipSafe == false {
            ConsoleCard("Run details", caption: "Coverage & costs") {
                if let costs {
                    Text("\(costs.calls.map(String.init) ?? "—") model calls · model \(costs.modelCostUsd.map { PrismFormat.money($0) } ?? "—") · total \(costs.totalCostUsd.map { PrismFormat.money($0) } ?? "—")")
                        .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                }
                if !failures.isEmpty {
                    DisclosureGroup("Failure reasons (\(failures.values.reduce(0, +)))") {
                        ForEach(failures.keys.sorted(), id: \.self) { reason in
                            HStack { Text(reason); Spacer(); Text("\(failures[reason] ?? 0)").monospacedDigit() }
                                .font(.system(size: 11)).foregroundStyle(.secondary).padding(.vertical, 3)
                        }.padding(.top, 6)
                    }.font(.system(size: 11))
                }
                if !gaps.isEmpty {
                    DisclosureGroup("Coverage gaps (\(gaps.count))") {
                        ForEach(gaps, id: \.self) { gap in
                            Label(gap, systemImage: "exclamationmark.triangle").font(.system(size: 11)).foregroundStyle(.orange).padding(.vertical, 3)
                        }.padding(.top, 6)
                    }.font(.system(size: 11))
                }
                if research?.coverage?.survivorshipSafe == false {
                    Label("Universe is not survivorship safe", systemImage: "exclamationmark.triangle")
                        .font(.system(size: 11)).foregroundStyle(.orange)
                }
            }
        }
    }
}
