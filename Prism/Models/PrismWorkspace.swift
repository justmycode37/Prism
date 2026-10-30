import Foundation

/// Workspace navigation is local; runtime mode changes remain explicit actions.
enum PrismWorkspace: String, CaseIterable, Identifiable {
    case singleTrader, agentSwarm

    var id: String { rawValue }
    var title: String { self == .singleTrader ? "Single SOL Trader" : "Agent Swarm" }
    var symbol: String { self == .singleTrader ? "chart.xyaxis.line" : "circle.hexagongrid" }
    var runtimeMode: String { self == .singleTrader ? "sol_perpetual" : "research" }

    func matches(selectedMode: String?) -> Bool {
        // Older runtime snapshots predate the SOL mode and are research-only.
        runtimeMode == (selectedMode ?? "research")
    }
}

/// Real, venue-provided one-minute OHLC bars. No quote interpolation.
struct SOLChartSnapshot: Decodable, Sendable {
    let symbol: String
    let category: String
    let timeframe: String
    let environment: String
    let candles: [SOLCandle]

    func displayCandles(for runtimeMode: String) -> [SOLCandle] {
        let expectedEnvironment = runtimeMode == "paper" ? "mainnet" : runtimeMode
        guard symbol == "SOLUSDT", category == "linear", timeframe == "1",
              ["mainnet", "testnet"].contains(expectedEnvironment), environment == expectedEnvironment else { return [] }
        var unique: [Int64: SOLCandle] = [:]
        for candle in candles where candle.isValid { unique[candle.startMs] = candle }
        return Array(unique.values.sorted { $0.startMs < $1.startMs }.suffix(120))
    }
}

struct SOLCandle: Decodable, Identifiable, Equatable, Sendable {
    var id: Int64 { startMs }
    let startMs: Int64
    let open: Double
    let high: Double
    let low: Double
    let close: Double
    let closed: Bool

    var date: Date { Date(timeIntervalSince1970: Double(startMs) / 1_000) }
    var centerDate: Date { date.addingTimeInterval(30) }
    var isRising: Bool { close >= open }
    var isValid: Bool {
        startMs >= 0 && startMs.isMultiple(of: 60_000)
            && [open, high, low, close].allSatisfy { $0.isFinite && $0 > 0 }
            && low <= min(open, close) && high >= max(open, close)
    }
}
