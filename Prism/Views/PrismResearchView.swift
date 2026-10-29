import SwiftUI

struct PrismResearchView: View {
    @ObservedObject var runtime: RuntimeClient
    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
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
}
