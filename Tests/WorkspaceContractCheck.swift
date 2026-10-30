import Foundation

@main
enum WorkspaceContractCheck {
    static func main() throws {
        precondition(PrismWorkspace.agentSwarm.matches(selectedMode: nil), "Legacy runtimes are research-only")
        precondition(PrismWorkspace.agentSwarm.matches(selectedMode: "research"))
        precondition(!PrismWorkspace.agentSwarm.matches(selectedMode: "sol_perpetual"))
        precondition(PrismWorkspace.singleTrader.matches(selectedMode: "sol_perpetual"))
        precondition(!PrismWorkspace.singleTrader.matches(selectedMode: nil))
        precondition(!PrismWorkspace.singleTrader.matches(selectedMode: "unknown"))

        let base: Int64 = 1_793_000_000_000 / 60_000 * 60_000
        func candle(_ index: Int, open: Double = 180, high: Double = 183, low: Double = 178,
                    close: Double = 182, closed: Bool = true) -> SOLCandle {
            SOLCandle(startMs: base + Int64(index) * 60_000, open: open, high: high, low: low, close: close, closed: closed)
        }
        func snapshot(_ candles: [SOLCandle], symbol: String = "SOLUSDT", category: String = "linear",
                      timeframe: String = "1", environment: String = "mainnet") -> SOLChartSnapshot {
            SOLChartSnapshot(symbol: symbol, category: category, timeframe: timeframe, environment: environment, candles: candles)
        }
        let up = candle(0)
        let down = candle(1, open: 182, close: 179)
        let doji = candle(2, close: 180)
        let forming = candle(3, closed: false)
        precondition(up.isRising && !down.isRising && doji.isValid)
        precondition(snapshot([forming, doji, up, down]).displayCandles(for: "paper") == [up, down, doji, forming],
                     "OHLC and forming status must be preserved in chronological order")
        precondition(snapshot([up]).displayCandles(for: "testnet").isEmpty, "Venue environments must stay isolated")
        precondition(snapshot([up], symbol: "BTCUSDT").displayCandles(for: "paper").isEmpty)
        precondition(snapshot([up], category: "spot").displayCandles(for: "paper").isEmpty)
        precondition(snapshot([up], timeframe: "5").displayCandles(for: "paper").isEmpty)
        precondition(snapshot([up], environment: "testnet").displayCandles(for: "testnet") == [up])
        let invalid = [candle(4, high: 179), candle(5, low: 181), candle(6, open: .nan),
                       candle(7, close: .infinity), candle(8, low: -1), candle(9, close: 0),
                       SOLCandle(startMs: base + 1, open: 180, high: 183, low: 178, close: 181, closed: true)]
        precondition(snapshot([up] + invalid).displayCandles(for: "paper") == [up], "Invalid candles must be omitted")
        let correction = candle(0, close: 181)
        precondition(snapshot([up, correction]).displayCandles(for: "paper") == [correction], "A revised bar retains one identity")
        let bounded = snapshot((0..<140).reversed().map { candle($0) }).displayCandles(for: "paper")
        precondition(bounded.count == 120 && bounded.first?.startMs == candle(20).startMs && bounded.last?.startMs == candle(139).startMs)
        precondition(up.date.timeIntervalSince1970 == Double(base) / 1_000)

        let json = """
        {"symbol":"SOLUSDT","category":"linear","timeframe":"1","environment":"mainnet",
         "candles":[{"start_ms":\(base),"open":180,"high":183,"low":178,"close":182,"closed":false}]}
        """
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let decoded = try decoder.decode(SOLChartSnapshot.self, from: Data(json.utf8))
        precondition(decoded.displayCandles(for: "paper").first?.closed == false)
        precondition(decoded.candles.first?.open == 180 && decoded.candles.first?.high == 183)
        print("PASS: Workspace isolation and real OHLC decoding, validation, corrections, ordering and bounded candle history.")
    }
}
