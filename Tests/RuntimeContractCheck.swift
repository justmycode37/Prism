import Foundation

/// Verifies runtime JSON and the leaderboard projection against native decoding contracts.
@main
struct RuntimeContractCheck {
    static func main() throws {
        guard CommandLine.arguments.count == 4 else { throw Failure.invalid("Pass research state, leaderboard and Sol state fixture paths.") }
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let leaderboardData = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let state = try decoder.decode(RuntimeState.self, from: data)
        let solData = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[3]))
        let sol = try decoder.decode(RuntimeState.self, from: solData)
        try require(state.tradingMode?.selected == "research" && sol.tradingMode?.isSolPerpetual == true,
                    "Both selectable mode states must decode without replacing Research.")
        try require(sol.tradingMode?.traderModel == "gpt-6.1-sol" && sol.tradingMode?.traderReasoningEffort == "xhigh" &&
                    sol.tradingMode?.supportModel == "gpt-6-luna" && sol.tradingMode?.supportReasoningEffort == "max" &&
                    sol.tradingMode?.swarmSize == 4,
                    "The Sol trader and Luna swarm must retain exact model routing.")
        try require(sol.tradingMode?.reviewerModel == "gpt-6-luna" &&
                    sol.tradingMode?.reviewerReasoningEffort == "max" && sol.tradingMode?.tradeReviewRequired == true,
                    "A dedicated Luna max reviewer must be mandatory in SOL mode.")
        var reviewedSol = try JSONSerialization.jsonObject(with: solData) as! [String: Any]
        var reviewedMode = reviewedSol["trading_mode"] as! [String: Any]
        reviewedMode["latest_trade_review"] = ["status": "rejected", "decision": "reject",
            "rationale": "Fixture proposal has expired.", "review_id": "fixture-review",
            "tool_name": "submit_order_intent", "created_at_ms": 1_793_347_200_000] as [String: Any]
        reviewedSol["trading_mode"] = reviewedMode
        let reviewed = try decoder.decode(RuntimeState.self, from: JSONSerialization.data(withJSONObject: reviewedSol))
        try require(reviewed.tradingMode?.latestTradeReview?.status == "rejected" &&
                    reviewed.tradingMode?.latestTradeReview?.rationale == "Fixture proposal has expired." &&
                    reviewed.tradingMode?.latestTradeReview?.createdAtMs == 1_793_347_200_000,
                    "The console must decode the recorded review decision independently of execution readiness.")
        try require(sol.tradingMode?.tradingPnl == 2 && sol.tradingMode?.tradingReturn == 0.02 &&
                    sol.tradingMode?.accountEquity == 102 && sol.tradingMode?.leverage == 5 &&
                    sol.tradingMode?.apiSpentUsd == 0 && sol.tradingMode?.lifetimeModelBudgetUsd == 400,
                    "Actual account trading P&L and lifetime API costs must decode separately.")
        try require(sol.tradingMode?.leveragePolicy == "agent_with_venue_ceiling" &&
                    sol.tradingMode?.venueMaxLeverage == nil && sol.tradingMode?.maxPositionRiskFraction == 0.01,
                    "Unknown venue leverage is absent and modeled dollar-risk controls are explicit.")
        try require(state.runtime.mode == "paper", "Paper execution must be explicit.")
        try require(state.runtime.status == "paused", "A new cascade must start paused.")
        try require(state.research?.historicalResultsAreDevelopment == true && state.research?.experimentCount == 0,
                    "Development evidence and the prospective registry must reach the console.")
        try require(!state.runtime.openaiConfigured, "The contract fixture must use no provider credential.")
        try require(state.operations?.mode == "paper" && state.operations?.enabled == false &&
                    state.operations?.sections.first?.items.first?.id == "prepare" &&
                    state.operations?.componentCosts.count == 4,
                    "Operational plans, explicit mode and component budgets must decode.")
        try require(state.agents.count == 33 && Set(state.agents.map(\.id)).count == 33,
                    "Every cascade agent must have a stable, unique UI identity.")
        try require(state.agents.filter { $0.model == "gpt-6-luna" }.count == 24,
                    "The default breadth layer must expose 24 Luna collectors.")
        try require(state.agents.filter { $0.model == "gpt-6.1-sol" }.count == 9,
                    "The analysis, strategy, and execution layers must expose nine Sol agents.")
        try require(state.agents.allSatisfy { $0.model != "gpt-6-astra" },
                    "The active roster must no longer route work to Astra.")
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
        legacy.removeValue(forKey: "operations")
        legacy.removeValue(forKey: "trading_mode")
        var legacyMarkets = legacy["markets"] as! [[String: Any]]
        legacyMarkets[0]["change_24h"] = NSNull()
        legacy["markets"] = legacyMarkets
        let legacyState = try decoder.decode(RuntimeState.self, from: JSONSerialization.data(withJSONObject: legacy))
        try require(legacyState.cascade == nil && legacyState.operations == nil && legacyState.markets[0].change24h == nil,
                    "Legacy metadata and unknown market changes must remain optional.")
        var noLeaderboard = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        var research = noLeaderboard["research"] as! [String: Any]
        research.removeValue(forKey: "leaderboard")
        noLeaderboard["research"] = research
        let oldResearchState = try decoder.decode(RuntimeState.self, from: JSONSerialization.data(withJSONObject: noLeaderboard))
        try require(oldResearchState.research?.leaderboard == nil,
                    "Older research snapshots without a leaderboard must still decode.")
        try require(state.portfolio.positions.first?.entryPrice == 100 && state.portfolio.trades.first?.id == "contract-fill",
                    "Nonempty paper positions and fills must decode.")
        try require(state.portfolio.totalPnl == 1 && state.portfolio.totalReturn == 0.0001,
                    "Portfolio P&L must use equity less initial capital.")
        try require(state.portfolio.pnlSamples.map(\.pnl) == [0, 1], "Actual P&L observations must remain intact.")
        try require(state.strategies.count == 2, "Scored and unscored cascade outcomes must remain visible.")
        let scored = state.strategies.first { $0.id == "scored" }
        let unscored = state.strategies.first { $0.id == "unscored" }
        try require(scored?.netReturn == -0.01 && scored?.spec?.parameters["lookback"] == 5,
                    "Negative measured outcomes and declarative parameters must decode.")
        try require(unscored?.sharpe == nil && unscored?.trades == nil,
                    "Unmeasured outcomes must preserve absent metrics.")
        try checkLeaderboard(leaderboardData, stateData: data, decoder: decoder)
        try require(!state.events.isEmpty, "Cascade events must reach the existing activity view.")
        try checkRouting(data)
        try checkConfiguration(state.config, decoder: decoder)
        try checkPnl(data, decoder: decoder)
        print("PASS: Research and Sol modes, model routing, separate trading/API metrics, leverage controls, four cascade layers, 33 research agents, leaderboard, paper records and configuration encoding.")
    }

    private static func checkLeaderboard(_ leaderboardData: Data, stateData: Data, decoder: JSONDecoder) throws {
        guard let leaderboardObject = try JSONSerialization.jsonObject(with: leaderboardData) as? [String: Any],
              var stateObject = try JSONSerialization.jsonObject(with: stateData) as? [String: Any],
              var researchObject = stateObject["research"] as? [String: Any] else {
            throw Failure.invalid("The offline leaderboard fixture must attach to a research runtime snapshot.")
        }
        researchObject["leaderboard"] = leaderboardObject
        stateObject["research"] = researchObject
        let fixtureState = try decoder.decode(RuntimeState.self, from: JSONSerialization.data(withJSONObject: stateObject))
        guard let leaderboard = fixtureState.research?.leaderboard else {
            throw Failure.invalid("The optional development leaderboard must decode when present.")
        }
        try require(leaderboard.total == 5 && leaderboard.ranked == 4 &&
                    leaderboard.rankingBasis == "development_net_return",
                    "Leaderboard summary and ranking basis must decode.")
        try require(leaderboard.testWindows == ["2025-01-01 → 2025-12-31"],
                    "Dated and undated results must preserve the selectable test period.")
        let rows = leaderboard.rankedRows
        try require(rows.map(\.id) == ["leader", "tie-first", "tie-second", "negative", "unmeasured"],
                    "Finite returns must rank descending, preserve stable ties, and put missing values last.")
        try require(rows.first?.name == "Top fixture" && rows.first?.symbol == "MRVL" &&
                    rows.first?.phase == "development" && rows.first?.netReturn == 0.12 &&
                    rows.first?.maxDrawdown == 0.025 && rows.first?.trades == 9,
                    "Leaderboard identity and measured strategy metrics must decode.")
        try require(rows.first?.qualification?.qualified == false &&
                    rows.first?.qualification?.status == "not_qualified" &&
                    rows.first?.qualification?.rankingEffect == "none",
                    "Development ranking must preserve its separate qualification status.")
        let missing = rows.last
        try require(missing?.finiteNetReturn == nil && missing?.finiteMaxDrawdown == nil && missing?.trades == nil,
                    "Unmeasured leaderboard values must remain absent rather than becoming zero.")
        let nonFinite = ResearchLeaderboardRow(
            id: "nonfinite", experimentId: nil, name: nil, symbol: nil, family: nil,
            phase: "development", netReturn: .infinity, maxDrawdown: .nan, trades: nil,
            screenStatus: nil, prospectiveStatus: nil, qualification: nil,
            forwardNetReturn: nil, forwardAsOf: nil, testStart: nil, testEnd: nil
        )
        try require(nonFinite.finiteNetReturn == nil && nonFinite.finiteMaxDrawdown == nil,
                    "Non-finite metrics must be treated as unavailable.")
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
