import Foundation

/// Verifies the cascade's real JSON state against the unchanged native UI model.
@main
struct RuntimeContractCheck {
    static func main() throws {
        guard CommandLine.arguments.count == 2 else { throw Failure.invalid("Pass the state fixture path.") }
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let state = try decoder.decode(RuntimeState.self, from: data)
        try require(state.runtime.mode == "paper", "Paper execution must be explicit.")
        try require(state.runtime.status == "paused", "A new cascade must start paused.")
        try require(state.research?.historicalResultsAreDevelopment == true && state.research?.experimentCount == 0,
                    "Development evidence and the prospective registry must reach the console.")
        try require(!state.runtime.openaiConfigured, "The contract fixture must use no provider credential.")
        try require(state.agents.count == 33 && Set(state.agents.map(\.id)).count == 33,
                    "Every cascade agent must have a stable, unique UI identity.")
        try require(state.agents.filter { $0.model == "gpt-6-luna" }.count == 24,
                    "The default breadth layer must expose 24 Luna collectors.")
        try require(state.agents.filter { $0.model == "gpt-6.1-sol" }.count == 6,
                    "The default analysis layer must expose six Sol analysts.")
        try require(state.agents.filter { $0.model == "gpt-6-astra" }.count == 3,
                    "The strategy and execution layers must expose three Astra agents.")
        try require(state.agents.allSatisfy { !($0.instructions ?? "").isEmpty },
                    "Each agent must expose its new layer instructions in the existing disclosure.")
        try require(state.agents.allSatisfy { $0.capabilities != nil && $0.recentTools != nil },
                    "Capabilities and observed tool activity must remain visible.")
        let agentIDs = Set(state.agents.map(\.id))
        try require(state.agents.allSatisfy { $0.parentId == nil || agentIDs.contains($0.parentId!) },
                    "The unchanged hierarchy renderer must receive valid reporting links.")
        try require(state.agentRoles?.count == 4, "The role catalog must contain the four cascade layers.")
        try require(state.markets.first?.change24h == 0.025 && state.markets.first?.fundingRate == nil,
                    "Equity daily changes and absent funding must decode.")
        try require(state.cascade?.sector?.id == "ai_infrastructure" && state.cascade?.collection?.collectorCount == 24,
                    "The dynamic sector and collection coverage must reach the console.")
        try require(state.cascade?.sector?.companies.first?.symbol == "MRVL" &&
                    state.cascade?.sector?.companies.first?.sourceUrls.count == 1,
                    "Discovered company identity and source provenance must decode.")
        var legacy = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        legacy.removeValue(forKey: "cascade")
        legacy.removeValue(forKey: "research")
        var legacyMarkets = legacy["markets"] as! [[String: Any]]
        legacyMarkets[0]["change_24h"] = NSNull()
        legacy["markets"] = legacyMarkets
        let legacyState = try decoder.decode(RuntimeState.self, from: JSONSerialization.data(withJSONObject: legacy))
        try require(legacyState.cascade == nil && legacyState.markets[0].change24h == nil,
                    "Legacy metadata and unknown market changes must remain optional.")
        try require(state.portfolio.positions.first?.entryPrice == 100 && state.portfolio.trades.first?.id == "contract-fill",
                    "Nonempty paper positions and fills must decode.")
        try require(state.portfolio.totalPnl == 1 && state.portfolio.totalReturn == 0.0001,
                    "The existing Overview must show equity less initial capital.")
        try require(state.portfolio.pnlSamples.map(\.pnl) == [0, 1], "Actual P&L observations must remain intact.")
        try require(state.strategies.count == 2, "Scored and unscored cascade outcomes must remain visible.")
        let scored = state.strategies.first { $0.id == "scored" }
        let unscored = state.strategies.first { $0.id == "unscored" }
        try require(scored?.netReturn == -0.01 && scored?.spec?.parameters["lookback"] == 5,
                    "Negative measured outcomes and declarative parameters must decode.")
        try require(unscored?.sharpe == nil && unscored?.trades == nil,
                    "Unmeasured outcomes must preserve absent metrics.")
        try require(!state.events.isEmpty, "Cascade events must reach the existing activity view.")
        try checkRouting(data)
        try checkConfiguration(state.config, decoder: decoder)
        try checkPnl(data, decoder: decoder)
        print("PASS: four cascade layers, 33 agents, model routing, unchanged Swift UI decoding, nullable strategy metrics, paper records, P&L, configuration encoding.")
    }

    private static func checkRouting(_ data: Data) throws {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let cascade = object["cascade"] as? [String: Any],
              let layers = cascade["layers"] as? [[String: Any]] else {
            throw Failure.invalid("Cascade routing metadata must accompany the unchanged UI fields.")
        }
        try require(layers.compactMap { $0["id"] as? String } == ["collector", "analyst", "strategist", "executor"],
                    "Layer metadata must preserve information-flow order.")
        try require(layers.compactMap { $0["count"] as? Int } == [24, 6, 2, 1],
                    "The default architecture must narrow at every handoff.")
        try require(layers.compactMap { $0["reasoning_effort"] as? String } == ["low", "medium", "high", "max"],
                    "Reasoning effort must increase toward execution.")
    }

    private static func checkConfiguration(_ configuration: RuntimeConfiguration, decoder: JSONDecoder) throws {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(configuration)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        try require(configuration.dailyLimitUsd == nil && object?["daily_limit_usd"] is NSNull,
                    "An off spending ceiling must encode as explicit null.")
        try require(configuration.maxCallsPerDay == nil && object?["max_calls_per_day"] is NSNull,
                    "An off call cap must encode as explicit null.")
        var changed = configuration
        changed.dailyLimitUsd = 250.125
        changed.maxCallsPerDay = 750
        let decoded = try decoder.decode(RuntimeConfiguration.self, from: encoder.encode(changed))
        try require(decoded.dailyLimitUsd == 250.125 && decoded.maxCallsPerDay == 750 &&
                    decoded.model == configuration.model && decoded.maxConcurrency == configuration.maxConcurrency &&
                    decoded.cycleIntervalSeconds == configuration.cycleIntervalSeconds,
                    "Native settings round trips must preserve concurrency, cadence and model.")
    }

    private static func checkPnl(_ data: Data, decoder: JSONDecoder) throws {
        let object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        var portfolio = object["portfolio"] as! [String: Any]
        portfolio["pnl_history"] = [
            ["timestamp": "2026-10-29T15:01:00.125Z", "pnl": 1.125],
            ["timestamp": "2026-10-29T15:00:00Z", "pnl": -0.125],
            ["timestamp": "2026-10-29T15:00:00Z", "pnl": -0.25],
        ]
        var decoded = try decoder.decode(PaperPortfolio.self, from: JSONSerialization.data(withJSONObject: portfolio))
        try require(decoded.pnlSamples.map(\.pnl) == [-0.125, -0.25, 1.125],
                    "Irregular timestamps and same-time changes must retain chronological order and precision.")
        portfolio["initial_equity"] = 0
        decoded = try decoder.decode(PaperPortfolio.self, from: JSONSerialization.data(withJSONObject: portfolio))
        try require(decoded.totalPnl == nil && decoded.totalReturn == nil,
                    "Invalid initial capital must not manufacture a P&L percentage.")
        try require(PrismFormat.date("2026-10-29T15:00:00Z") != nil &&
                    PrismFormat.date("2026-10-29T15:00:00.123Z") != nil,
                    "Whole and fractional timestamp forms must decode.")
    }

    private static func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw Failure.invalid(message) }
    }
    private enum Failure: Error { case invalid(String) }
}
