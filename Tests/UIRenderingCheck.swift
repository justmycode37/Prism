import AppKit
import SwiftUI

fileprivate enum SingleTraderAccountFixture: String, CaseIterable {
    case normal = "normal"
    case zeroEquityLoss = "zero-equity-loss"
    case unavailableEquity = "unavailable-equity"
    case offlineRetainedPnl = "offline-retained-pnl"
    case priorUTCDay = "prior-utc-day"
}

enum PrismAppearance: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .system: "circle.lefthalf.filled"
        case .light: "sun.max"
        case .dark: "moon"
        }
    }
}

@MainActor
final class AppearancePreferences: ObservableObject {
    static let blurRange = 0.0...100.0
    @Published var selection: PrismAppearance = .system
    @Published var backdropBlurRadius = 40.0
    @Published var milkiness = 0.0
    func apply() {}
}

@MainActor
final class RuntimeClient: ObservableObject {
    @Published var state: RuntimeState?
    @Published var isConnected = false
    @Published var isBusy = false
    @Published var errorMessage: String? = "Offline rendering fixture; no runtime connection attempted."
    @Published var lastUpdated: Date? = nil
    @Published var selectedPage: PrismPage = .overview
    @Published var pnlSamples: [PaperPnlSample] = []

    fileprivate init(includeSnapshot: Bool, singleTraderFixture: Bool = false, solSampleCount: Int = 0,
                     accountFixture: SingleTraderAccountFixture = .normal) {
        if includeSnapshot {
            let json = singleTraderFixture ? Self.singleTraderFixture(solSampleCount: solSampleCount,
                accountFixture: accountFixture) : Self.fixture
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            do {
                state = try decoder.decode(RuntimeState.self, from: Data(json.utf8))
            } catch {
                fatalError("Offline fixture failed to decode: \(error)")
            }
            guard let snapshot = state,
                  snapshot.agents.count >= 4, snapshot.agentRoles?.count ?? 0 >= 4,
                  snapshot.strategies.count >= 6, snapshot.markets.count >= 4,
                  snapshot.events.count >= 3, snapshot.portfolio.positions.count >= 2,
                  snapshot.portfolio.trades.count >= 2,
                  snapshot.research?.leaderboard?.rows?.count ?? 0 >= 6,
                  snapshot.research?.forwardReports?.count ?? 0 >= 2,
                  snapshot.operations?.sections.count ?? 0 >= 2,
                  snapshot.operations?.componentCosts.count ?? 0 >= 4 else {
                fatalError("Offline fixture decoded but is missing representative visual content")
            }
            if singleTraderFixture {
                guard snapshot.tradingMode?.isSolPerpetual == true,
                      snapshot.markets.contains(where: { $0.symbol == "SOLUSDT" }),
                      snapshot.tradingMode?.actualSolExecution?.fills?.contains(where: { $0.id == "sol-exec-1" }) == true,
                      !snapshot.portfolio.positions.contains(where: { $0.symbol == "SOLUSDT" }),
                      !snapshot.portfolio.trades.contains(where: { $0.symbol == "SOLUSDT" }),
                      snapshot.agents.contains(where: { $0.roleId == "sol_trader" }) else {
                    fatalError("SOL trader fixture must use the actual execution projection, not research simulation")
                }
                switch accountFixture {
                case .normal:
                    guard snapshot.tradingMode?.actualSolExecution?.positions?.contains(where: { $0.symbol == "SOLUSDT" }) == true else {
                        fatalError("Normal SOL rendering fixture must include actual account exposure")
                    }
                case .offlineRetainedPnl:
                    guard snapshot.tradingMode?.accountEquity != nil,
                          snapshot.tradingMode?.tradingPnl != nil,
                          snapshot.tradingMode?.accountDay == "2026-10-30" else {
                        fatalError("Offline SOL fixture must retain its recorded account metrics")
                    }
                case .priorUTCDay:
                    guard snapshot.tradingMode?.accountEquity != nil,
                          snapshot.tradingMode?.tradingPnl != nil,
                          snapshot.tradingMode?.accountDay == "2026-10-29" else {
                        fatalError("Rollover fixture must retain the prior UTC-day account metrics")
                    }
                case .zeroEquityLoss:
                    guard snapshot.tradingMode?.accountEquity == 0,
                          snapshot.tradingMode?.accountEquity.map({ PrismFormat.money($0) }) == PrismFormat.money(0),
                          snapshot.tradingMode?.tradingPnl == -1_038.6,
                          snapshot.tradingMode?.actualSolExecution?.confirmedFlat == false else {
                        fatalError("Zero-equity account loss must remain distinct from missing equity and confirmed flatness")
                    }
                case .unavailableEquity:
                    guard snapshot.tradingMode?.accountEquity == nil,
                          snapshot.tradingMode?.tradingPnl == nil,
                          snapshot.tradingMode?.accountStatus == "stale",
                          snapshot.tradingMode?.accountAsOf != nil,
                          snapshot.tradingMode?.actualSolExecution?.confirmedFlat == false else {
                        fatalError("Unavailable current equity must not be replaced by a saved baseline or confirmed flatness")
                    }
                }
                if [.zeroEquityLoss, .unavailableEquity].contains(accountFixture) {
                    guard snapshot.tradingMode?.actualSolExecution?.positionsStatus == "unavailable",
                          snapshot.tradingMode?.actualSolExecution?.positions?.isEmpty == true,
                          snapshot.tradingMode?.enabled == false,
                          snapshot.tradingMode?.pending == true,
                          snapshot.operations?.riskStatus == "reconciliation_required" else {
                        fatalError("Account-loss fixtures must remain unreconciled and unconfirmed flat")
                    }
                }
                let fixtureNow = accountFixture == .priorUTCDay
                    ? PrismFormat.date("2026-10-30T00:00:05Z")!
                    : PrismFormat.date("2026-10-30T10:01:00Z")!
                let connected = accountFixture != .offlineRetainedPnl
                let isCurrent = TradingAccountPresentation.isCurrent(
                    snapshot.tradingMode!, connected: connected, now: fixtureNow)
                if let asOf = snapshot.tradingMode?.accountAsOf, let recordedAt = PrismFormat.date(asOf) {
                    guard !TradingAccountPresentation.isCurrent(snapshot.tradingMode!, connected: true,
                        now: recordedAt.addingTimeInterval(-1)) else {
                        fatalError("A future account timestamp cannot be presented as current")
                    }
                }
                if accountFixture == .zeroEquityLoss {
                    guard isCurrent,
                          TradingAccountPresentation.pnlTitle(snapshot.tradingMode!, current: isCurrent) == "Trading P&L · today",
                          TradingAccountPresentation.equityTitle(current: isCurrent) == "Account equity" else {
                        fatalError("A connected, current-day zero balance must retain its recorded-current labels")
                    }
                }
                if accountFixture == .offlineRetainedPnl || accountFixture == .priorUTCDay {
                    guard !isCurrent,
                          TradingAccountPresentation.pnlTitle(snapshot.tradingMode!, current: isCurrent) == "Trading P&L · recorded",
                          TradingAccountPresentation.equityTitle(current: isCurrent) == "Account equity · snapshot" else {
                        fatalError("Disconnected or prior-day values must be labeled as recorded snapshots")
                    }
                }
            }
            isConnected = true
            if accountFixture == .offlineRetainedPnl { isConnected = false }
            errorMessage = nil
            pnlSamples = state?.portfolio.pnlSamples ?? []
        }
    }

    func monitor() async { /* Deliberately inert: never contacts the service. */ }
    func control(_ action: String) async { /* Test-only no-op. */ }
    func updateDailySpendCeiling(_ amount: Double?) async -> Bool { false }
    func selectTradingMode(_ mode: String) async -> Bool { false }

    fileprivate static func singleTraderFixture(solSampleCount: Int,
                                                accountFixture: SingleTraderAccountFixture = .normal) -> String {
        let data = Data(fixture.utf8)
        guard var root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              var agents = root["agents"] as? [[String: Any]],
              var markets = root["markets"] as? [[String: Any]] else {
            fatalError("Could not prepare local SOL trader rendering fixture")
        }
        root["trading_mode"] = [
            "selected": "sol_perpetual", "trader_model": "offline-fixture",
            "trader_reasoning_effort": "medium", "support_model": "offline-fixture",
            "reviewer_model": "gpt-6-luna", "reviewer_reasoning_effort": "max", "trade_review_required": true,
            "support_reasoning_effort": "low", "symbol": "SOLUSDT", "category": "linear",
            "primary_timeframe": "5m", "continuous": true, "pnl_basis": "realized_plus_unrealized",
            "lifetime_model_budget_usd": 12.0, "trading_pnl": 38.6, "trading_return": 0.0386,
            "account_equity": 1_038.6, "gross_exposure": 720.0, "leverage": 0.72,
            "leverage_policy": "capped", "venue_max_leverage": 20.0,
            "account_as_of": "2026-10-30T10:00:00Z", "account_day": "2026-10-30",
            "account_status": "current",
            "max_position_risk_fraction": 0.02, "enabled": true,
            "api_spent_usd": 1.18, "api_reserved_usd": 0.22, "pending": false,
            "actual_sol_execution": [
                "environment": "paper", "account_scope": "opaque-fixture-scope",
                "positions_status": "confirmed", "positions_as_of": "2026-10-30T10:00:00Z",
                "confirmed_flat": false,
                "positions": [["id": "sol-position-fixture", "category": "linear",
                    "symbol": "SOLUSDT", "side": "long", "quantity": 4.0,
                    "entry_price": 174.25, "mark_price": 179.8, "leverage": 2.0,
                    "stop_loss": 168.0, "ownership": "managed"]],
                "fills_available": true, "fills_truncated": false,
                "fills": [["id": "sol-exec-1", "side": "buy", "quantity": 4.0,
                    "price": 174.25, "timestamp": "2026-10-30T09:15:00Z",
                    "fee": 0.4182, "fee_currency": "USDT", "realized_pnl": 0.0]]
            ] as [String: Any]
        ]
        markets.append(["symbol": "SOLUSDT", "price": 179.8, "change_24h": 0.027,
                        "funding_rate": 0.0001, "updated_at": "2026-10-30T10:00:00Z"])
        root["markets"] = markets
        if solSampleCount > 0 {
            let candles = syntheticSOLCandles()
            root["sol_chart"] = [
                "symbol": "SOLUSDT", "category": "linear", "timeframe": "1",
                "environment": "mainnet", "candles": Array(candles.suffix(min(solSampleCount, candles.count)))
            ]
        } else {
            root.removeValue(forKey: "sol_chart")
        }
        if [.zeroEquityLoss, .unavailableEquity].contains(accountFixture) {
            if var mode = root["trading_mode"] as? [String: Any] {
                mode["enabled"] = false
                mode["pending"] = true
                mode["unsupported_reason"] = "Synthetic fixture: account reconciliation is required."
                if var actual = mode["actual_sol_execution"] as? [String: Any] {
                    actual["positions_status"] = "unavailable"
                    actual["positions_as_of"] = NSNull()
                    actual["confirmed_flat"] = false
                    actual["positions"] = []
                    mode["actual_sol_execution"] = actual
                }
                switch accountFixture {
                case .normal:
                    break
                case .zeroEquityLoss:
                    mode["account_equity"] = 0.0
                    mode["trading_pnl"] = -1_038.6
                    mode["trading_return"] = -1.0
                    mode["account_status"] = "current"
                    mode["account_as_of"] = "2026-10-30T10:00:00Z"
                    mode["account_day"] = "2026-10-30"
                case .unavailableEquity:
                    mode["account_equity"] = NSNull()
                    mode["trading_pnl"] = NSNull()
                    mode["trading_return"] = NSNull()
                    mode["account_as_of"] = "2026-10-29T10:00:00Z"
                    mode["account_day"] = "2026-10-29"
                    mode["account_status"] = "stale"
                    // Retained host accounting state must not be mistaken for
                    // current equity when the live value is unavailable.
                    mode["daily_baseline_equity"] = 1_038.6
                case .offlineRetainedPnl, .priorUTCDay:
                    break
                }
                root["trading_mode"] = mode
            }
            if var operations = root["operations"] as? [String: Any] {
                operations["risk_status"] = "reconciliation_required"
                root["operations"] = operations
            }
        } else if accountFixture == .priorUTCDay, var mode = root["trading_mode"] as? [String: Any] {
            // The source was fresh 35 seconds ago; the UTC day rolled while
            // the service snapshot remained connected and otherwise current.
            mode["account_as_of"] = "2026-10-29T23:59:30Z"
            mode["account_day"] = "2026-10-29"
            mode["account_status"] = "current"
            root["trading_mode"] = mode
        }
        agents.append(["id": "agent-sol-trader", "name": "SOL Perpetual Trader", "team": "Trading",
                       "status": "working", "task": "Manage paper SOLUSDT position", "last_run_at": "2026-10-30T09:58:00Z",
                       "summary": "Paper trader is monitoring the 5 minute SOLUSDT setup; no live orders are enabled.",
                       "role_id": "sol_trader", "parent_id": "orchestrator", "kind": "worker",
                       "mandate": "Trade SOL perpetuals in paper mode with bounded risk.", "model": "offline-fixture",
                       "reasoning_effort": "medium"])
        root["agents"] = agents
        guard let encoded = try? JSONSerialization.data(withJSONObject: root, options: [.sortedKeys]),
              let json = String(data: encoded, encoding: .utf8) else {
            fatalError("Could not encode local SOL trader rendering fixture")
        }
        return json
    }

    private static func syntheticSOLCandles() -> [[String: Any]] {
        let formatter = ISO8601DateFormatter()
        let quoteTime = formatter.date(from: "2026-10-30T10:00:00Z")!.timeIntervalSince1970 * 1_000
        var previousClose = 178.65
        return (0..<120).map { index in
            let open = previousClose
            var close = 179.0 + sin(Double(index) * 0.47) * 0.72 + cos(Double(index) * 0.21) * 0.28
            if [9, 23, 37].contains(index) { close = open }
            if index == 119 { close = 179.8 }
            let upperWick = 0.08 + Double(index % 4) * 0.025
            let lowerWick = 0.07 + Double(index % 3) * 0.03
            let start = quoteTime - Double(120 - index) * 60_000
            let candle: [String: Any] = [
                "start_ms": Int64(start), "open": open,
                "high": max(open, close) + upperWick,
                "low": min(open, close) - lowerWick,
                "close": close, "closed": index < 119
            ]
            previousClose = close
            return candle
        }
    }

    fileprivate static let fixture = #"""
    {
      "runtime": {"status":"running","mode":"paper","started_at":"2026-10-30T08:00:00Z","next_cycle_at":"2026-10-30T10:05:00Z","openai_configured":false,"last_error":null,"credentials_ready":false},
      "budget": {"daily_limit_usd":25,"spent_today_usd":2.45,"reserved_usd":0.8,"calls_today":8,"max_calls_per_day":120},
      "agent_roles": [
        {"id":"analyst","name":"Market Analyst","team":"Research","kind":"worker","researcher":true,"mandate":"Compare public market data with the current research universe. Summarize evidence, cite source dates, and report missing coverage before proposing a conclusion.","template_role_id":"analyst","instruction_path":"backend/agent_roles/analyst.md","instructions":"Review evidence and provide a concise dated report.","is_dynamic":false,"created_by":"orchestrator","created_at":"2026-10-30T08:01:00Z"},
        {"id":"collector","name":"Evidence Collector","team":"Research","kind":"worker","researcher":true,"mandate":"Collect public evidence for AI infrastructure companies and preserve source URLs, evidence IDs, and collection timestamps.","template_role_id":"collector","instruction_path":"backend/agent_roles/collector.md","is_dynamic":false,"created_by":"orchestrator","created_at":"2026-10-30T08:01:00Z"},
        {"id":"strategist","name":"Strategy Designer","team":"Research","kind":"worker","researcher":true,"mandate":"Create paper strategy hypotheses from available evidence and label all historical results as development evidence.","template_role_id":"strategist","instruction_path":"backend/agent_roles/strategist.md","is_dynamic":false,"created_by":"orchestrator","created_at":"2026-10-30T08:01:00Z"},
        {"id":"executor","name":"Paper Executor","team":"Operations","kind":"worker","researcher":false,"mandate":"Reconcile paper positions, apply risk controls, and explain every simulated action in the activity log.","template_role_id":"executor","instruction_path":"backend/agent_roles/executor.md","is_dynamic":false,"created_by":"orchestrator","created_at":"2026-10-30T08:01:00Z"}
      ],
      "agents": [
        {"id":"agent-analyst-01","name":"Semiconductor Analyst","team":"Research","status":"working","task":"Review accelerator demand and inventory signals","last_run_at":"2026-10-30T10:02:30Z","summary":"Latest report: demand remains broad across data-center accelerators; inventory normalization is a key uncertainty for the next quarter.","role_id":"analyst","parent_id":"orchestrator","kind":"worker","mandate":"Assess sector signals and communicate uncertainty.","created_by":"orchestrator","created_at":"2026-10-30T08:05:00Z","creation_reason":"Scheduled sector review","instruction_path":"backend/agent_roles/analyst.md","is_dynamic":false,"model":"offline-fixture","reasoning_effort":"medium","capabilities":[{"id":"market-review","name":"Market review","description":"Compare public prices and dated market observations.","available":true,"reason":null},{"id":"provider-call","name":"Provider inference","description":"External model access is disabled in this rendering fixture.","available":false,"reason":"Credentials intentionally unavailable"}],"recent_tools":[{"id":"tool-1","name":"lookup_market_quotes","status":"completed","summary":"Read local synthetic market fixture for four symbols.","created_at":"2026-10-30T10:02:00Z","duration_ms":420}]},
        {"id":"agent-collector-01","name":"AI Infrastructure Collector","team":"Research","status":"completed","task":"Collect dated public evidence for the sector universe","last_run_at":"2026-10-30T09:58:00Z","summary":"Latest output: 18 evidence records from 7 public sources; two cooling suppliers still need primary-source coverage.","role_id":"collector","parent_id":"orchestrator","kind":"worker","mandate":"Keep source provenance attached to every collected record.","created_by":"orchestrator","created_at":"2026-10-30T08:06:00Z","creation_reason":"Evidence gap identified","instruction_path":"backend/agent_roles/collector.md","is_dynamic":false,"model":"offline-fixture","reasoning_effort":"low","capabilities":[],"recent_tools":[{"id":"tool-2","name":"search_public_sources","status":"completed","summary":"Collected synthetic fixture records; no network request was made.","created_at":"2026-10-30T09:57:30Z","duration_ms":890}]},
        {"id":"agent-strategist-01","name":"Momentum Hypothesis","team":"Research","status":"idle","task":"Refine paper entry and exit assumptions","last_run_at":"2026-10-30T09:42:00Z","summary":"Latest output: retain the hypothesis for development review; forward evidence is not yet sufficient for qualification.","role_id":"strategist","parent_id":"orchestrator","kind":"worker","mandate":"Separate historical development performance from prospective evidence.","created_by":"orchestrator","created_at":"2026-10-30T08:07:00Z","creation_reason":"Candidate creation","instruction_path":"backend/agent_roles/strategist.md","is_dynamic":false,"model":"offline-fixture","reasoning_effort":"medium"},
        {"id":"agent-executor-01","name":"Paper Portfolio Reconciler","team":"Operations","status":"working","task":"Reconcile simulated equity and open positions","last_run_at":"2026-10-30T10:00:00Z","summary":"Latest output: two positions reconciled; estimated fees are included in paper equity.","role_id":"executor","parent_id":"orchestrator","kind":"worker","mandate":"Never submit live orders; reconcile paper activity and report risk limits.","created_by":"orchestrator","created_at":"2026-10-30T08:08:00Z","creation_reason":"Portfolio review","instruction_path":"backend/agent_roles/executor.md","is_dynamic":false,"model":"offline-fixture","reasoning_effort":"low"}
      ],
      "strategies": [
        {"id":"exp-001","name":"AI infrastructure trend continuation","symbol":"NVDA","family":"momentum","status":"promising","net_return":0.184,"sharpe":1.42,"max_drawdown":0.112,"trades":38,"reason":"Positive development return with moderate drawdown; requires a longer prospective observation window.","created_at":"2026-10-10T12:00:00Z","agent_id":"agent-strategist-01","phase":"development","experiment_id":"run-2026-10-10-a"},
        {"id":"exp-002","name":"Broad semiconductor pullback recovery","symbol":"AMD","family":"mean_reversion","status":"review","net_return":0.071,"sharpe":0.88,"max_drawdown":0.164,"trades":22,"reason":"Positive return, though concentrated in a small number of recovery sessions.","created_at":"2026-10-13T12:00:00Z","agent_id":"agent-strategist-01","phase":"development","experiment_id":"run-2026-10-13-b"},
        {"id":"exp-003","name":"Long-horizon networking equipment breakout hypothesis","symbol":"AVGO","family":"breakout","status":"promising","net_return":0.126,"sharpe":1.11,"max_drawdown":0.139,"trades":31,"reason":"Needs independent validation across additional market regimes.","created_at":"2026-10-16T12:00:00Z","agent_id":"agent-strategist-01","phase":"development","experiment_id":"run-2026-10-16-c"},
        {"id":"exp-004","name":"Power and cooling relative strength","symbol":"VRT","family":"momentum","status":"rejected","net_return":-0.083,"sharpe":-0.41,"max_drawdown":0.207,"trades":19,"reason":"Negative development result; turnover and drawdown exceed the current paper risk budget.","created_at":"2026-10-19T12:00:00Z","agent_id":"agent-strategist-01","phase":"development","experiment_id":"run-2026-10-19-d"},
        {"id":"exp-005","name":"Cloud compute quality adjusted trend","symbol":"AMZN","family":"trend","status":"awaiting_data","net_return":null,"sharpe":null,"max_drawdown":null,"trades":null,"reason":"Awaiting enough observations to measure this candidate.","created_at":"2026-10-22T12:00:00Z","agent_id":"agent-strategist-01","phase":"development","experiment_id":"run-2026-10-22-e"},
        {"id":"exp-006","name":"Memory suppliers earnings drift","symbol":"MU","family":"event_drift","status":"promising","net_return":0.043,"sharpe":0.67,"max_drawdown":0.091,"trades":12,"reason":"Small sample; keep visible without treating it as a qualified result.","created_at":"2026-10-25T12:00:00Z","agent_id":"agent-strategist-01","phase":"development","experiment_id":"run-2026-10-25-f"}
      ],
      "portfolio": {"equity":10250,"cash":9100,"realized_pnl":120,"unrealized_pnl":130,"initial_equity":10000,"as_of":"2026-10-30T10:00:00Z","pnl_history":[{"timestamp":"2026-10-29T09:00:00Z","pnl":72},{"timestamp":"2026-10-29T15:00:00Z","pnl":96},{"timestamp":"2026-10-30T10:00:00Z","pnl":250}],"positions":[{"symbol":"NVDA","side":"long","quantity":2,"entry_price":131.5,"mark_price":136.25,"unrealized_pnl":9.5},{"symbol":"AVGO","side":"long","quantity":1,"entry_price":171.0,"mark_price":168.5,"unrealized_pnl":-2.5}],"trades":[{"id":"trade-1","symbol":"NVDA","side":"buy","quantity":2,"price":131.5,"timestamp":"2026-10-29T12:00:00Z","realized_pnl":null},{"id":"trade-2","symbol":"AMD","side":"sell","quantity":1,"price":161.2,"timestamp":"2026-10-30T09:00:00Z","realized_pnl":12.4}],"valuation_complete":true,"risk_halted":false,"warnings":["Synthetic paper fixture; values are for visual review only."],"receivables":[{"id":"income-1","amount":3.25,"payable_at_ms":1793400000000}]},
      "markets": [
        {"symbol":"NVDA","price":136.25,"change_24h":0.023,"funding_rate":0.0001,"updated_at":"2026-10-30T10:00:00Z"},
        {"symbol":"AMD","price":159.80,"change_24h":-0.014,"funding_rate":-0.0002,"updated_at":"2026-10-30T09:59:00Z"},
        {"symbol":"AVGO","price":168.50,"change_24h":0.008,"funding_rate":0.0001,"updated_at":"2026-10-30T09:58:00Z"},
        {"symbol":"VRT","price":141.10,"change_24h":-0.031,"funding_rate":0.0,"updated_at":"2026-10-30T09:57:00Z"}
      ],
      "events": [
        {"id":101,"timestamp":"2026-10-30T10:00:00Z","level":"info","source":"paper_exchange","message":"Portfolio valuation reconciled for two synthetic open positions."},
        {"id":102,"timestamp":"2026-10-30T09:58:00Z","level":"warning","source":"research","message":"Cooling sector coverage has two outstanding primary-source gaps."},
        {"id":103,"timestamp":"2026-10-30T09:42:00Z","level":"info","source":"strategist","message":"Development candidate retained for prospective review."}
      ],
      "config": {"model":"offline-fixture","max_concurrency":2,"cycle_interval_seconds":60,"daily_limit_usd":25,"max_calls_per_day":120},
      "cascade": {
        "layers":[{"id":"research","name":"Research","count":3,"reasoning_effort":"medium","model":"offline-fixture"}],
        "sector":{"id":"ai-infrastructure","name":"AI Infrastructure","companies":[{"symbol":"NVDA","name":"NVIDIA","themes":["accelerators","data_center"],"source_urls":["https://example.invalid/nvda"],"evidence_ids":["evidence-1"],"discovery_reason":"Synthetic fixture company for layout review."},{"symbol":"AVGO","name":"Broadcom","themes":["networking","custom_silicon"],"source_urls":["https://example.invalid/avgo"],"evidence_ids":["evidence-2"],"discovery_reason":"Synthetic fixture company for layout review."}],"symbols":["NVDA","AVGO"],"discovery_status":"complete"},
        "collection":{"evidence_count":18,"source_count":7,"collector_count":4,"active_collector_count":2}
      },
      "research": {
        "phase":"development","historical_results_are_development":true,"experiment_count":6,
        "statuses":{"promising":3,"review":1,"rejected":1,"awaiting_data":1},
        "failure_reasons":{"insufficient_history":2,"source_coverage_gap":1},
        "operating_costs":{"model_cost_usd":2.45,"total_cost_usd":3.10,"calls":8},
        "coverage":{"gaps":["Cooling suppliers need primary-source confirmation","One candidate has fewer than twenty observations"],"survivorship_safe":false},
        "forward_reports":[
          {"experiment_id":"run-2026-10-10-a","status":"observing","sessions":14,"net_return":0.031,"excess_return":0.012,"operating_pnl_usd":28.5,"reasons":["Observation window remains open","No qualification decision yet"]},
          {"experiment_id":"run-2026-10-19-d","status":"insufficient_data","sessions":5,"net_return":-0.018,"excess_return":-0.026,"operating_pnl_usd":-8.2,"reasons":["Too few prospective sessions"]}
        ],
        "leaderboard":{"rows":[
          {"id":"lb-1","experiment_id":"run-2026-10-10-a","name":"AI infrastructure trend continuation","symbol":"NVDA","family":"momentum","phase":"development","net_return":0.184,"max_drawdown":0.112,"trades":38,"screen_status":"passed","prospective_status":"observing","qualification":{"qualified":false,"status":"pending","basis":"forward_observations","ranking_effect":"development_only","reasons":["Prospective window is still open"]},"forward_net_return":0.031,"forward_as_of":"2026-10-30T10:00:00Z","test_start":"2026-08-01","test_end":"2026-09-30"},
          {"id":"lb-2","experiment_id":"run-2026-10-16-c","name":"Long-horizon networking equipment breakout hypothesis","symbol":"AVGO","family":"breakout","phase":"development","net_return":0.126,"max_drawdown":0.139,"trades":31,"screen_status":"passed","prospective_status":"pending","test_start":"2026-07-01","test_end":"2026-09-30"},
          {"id":"lb-3","experiment_id":"run-2026-10-13-b","name":"Broad semiconductor pullback recovery","symbol":"AMD","family":"mean_reversion","phase":"development","net_return":0.071,"max_drawdown":0.164,"trades":22,"screen_status":"review","prospective_status":"insufficient_data","test_start":"2026-06-01","test_end":"2026-08-31"},
          {"id":"lb-4","experiment_id":"run-2026-10-25-f","name":"Memory suppliers earnings drift","symbol":"MU","family":"event_drift","phase":"development","net_return":0.043,"max_drawdown":0.091,"trades":12,"screen_status":"review","test_start":"2026-09-01","test_end":"2026-10-15"},
          {"id":"lb-5","experiment_id":"run-2026-10-19-d","name":"Power and cooling relative strength","symbol":"VRT","family":"momentum","phase":"development","net_return":-0.083,"max_drawdown":0.207,"trades":19,"screen_status":"rejected","prospective_status":"insufficient_data","test_start":"2026-05-01","test_end":"2026-07-31"},
          {"id":"lb-6","experiment_id":"run-2026-10-22-e","name":"Cloud compute quality adjusted trend with an intentionally long research title for truncation review","symbol":"AMZN","family":"trend","phase":"development","net_return":null,"max_drawdown":null,"trades":null,"screen_status":"awaiting_data","prospective_status":"pending","test_start":"2026-10-01","test_end":"2026-10-29"}
        ],"total":6,"ranked":5,"ranking_basis":"development_net_return","limit":10}
      },
      "operations":{"mode":"paper","enabled":true,"next_wake_at":"2026-10-30T10:05:00Z","rationale":"Synthetic scheduled review; no service is running.","risk_status":"clear","gross_exposure":1150,"realized_pnl":120,"unrealized_pnl":130,
        "sections":[
          {"id":"market-data","title":"Market data","items":[{"id":"quotes","title":"Synthetic quote fixture","detail":"Four local market observations loaded for visual review.","status":"ready","time":"2026-10-30T10:00:00Z","source_url":null}]},
          {"id":"risk-controls","title":"Risk controls","items":[{"id":"daily-budget","title":"Daily paper budget","detail":"USD 2.45 spent of USD 25.00; no external calls are enabled.","status":"clear","time":"2026-10-30T10:00:00Z","source_url":null}]}
        ],
        "component_costs":[{"id":"analyst","calls":3,"spent_usd":0.85,"reserved_usd":0.25,"limit_usd":8},{"id":"collector","calls":2,"spent_usd":0.55,"reserved_usd":0.2,"limit_usd":5},{"id":"strategist","calls":2,"spent_usd":0.75,"reserved_usd":0.25,"limit_usd":7},{"id":"executor","calls":1,"spent_usd":0.3,"reserved_usd":0.1,"limit_usd":5}]
      }
    }
    """#
}

@MainActor
@main
enum UIRenderingCheck {
    static func main() throws {
        _ = NSApplication.shared
        let output = URL(fileURLWithPath: "/tmp/prism-ui-redesign", isDirectory: true)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let pages: [PrismPage] = [.overview, .research, .test, .execution, .agents, .settings]
        var failures: [String] = []
        var rendered = 0
        for page in pages {
            for appearance in [("light", NSAppearance.Name.aqua, ColorScheme.light), ("dark", NSAppearance.Name.darkAqua, ColorScheme.dark)] {
                for size in [(840, 560, "large"), (600, 420, "minimum")] {
                    let name = "\(page.rawValue)-\(appearance.0)-\(size.2).png"
                    do {
                        try render(page: page, appearance: appearance.1, colorScheme: appearance.2,
                                   size: NSSize(width: size.0, height: size.1), includeSnapshot: true,
                                   initialWorkspace: .agentSwarm,
                                   destination: output.appendingPathComponent(name))
                        rendered += 1
                    } catch { failures.append("\(name): \(error)") }
                }
            }
            let name = "\(page.rawValue)-offline-empty.png"
            do {
                try render(page: page, appearance: .aqua, colorScheme: .light,
                           size: NSSize(width: 840, height: 560), includeSnapshot: false,
                           initialWorkspace: .agentSwarm,
                           destination: output.appendingPathComponent(name))
                rendered += 1
            } catch { failures.append("\(name): \(error)") }
        }
        for appearance in [("light", NSAppearance.Name.aqua, ColorScheme.light), ("dark", NSAppearance.Name.darkAqua, ColorScheme.dark)] {
            for size in [(840, 560, "large"), (600, 420, "minimum")] {
                let name = "workspace-chooser-\(appearance.0)-\(size.2).png"
                do {
                    try render(appearance: appearance.1, colorScheme: appearance.2,
                               size: NSSize(width: size.0, height: size.1), includeSnapshot: false,
                               initialWorkspace: nil, destination: output.appendingPathComponent(name))
                    rendered += 1
                } catch { failures.append("\(name): \(error)") }
                let traderName = "single-trader-\(appearance.0)-\(size.2).png"
                do {
                    try render(appearance: appearance.1, colorScheme: appearance.2,
                               size: NSSize(width: size.0, height: size.1), includeSnapshot: true,
                                   singleTraderFixture: true, solSampleCount: 120,
                               initialWorkspace: .singleTrader, destination: output.appendingPathComponent(traderName))
                    rendered += 1
                } catch { failures.append("\(traderName): \(error)") }
            }
        }
        for (name, snapshot, samples) in [("single-trader-offline-empty", false, 0),
                                          ("single-trader-single-candle", true, 1),
                                          ("single-trader-populated-chart", true, 120)] {
            do {
                try render(appearance: .aqua, colorScheme: .light, size: NSSize(width: 840, height: 560),
                           includeSnapshot: snapshot, singleTraderFixture: snapshot, solSampleCount: samples,
                           initialWorkspace: .singleTrader, destination: output.appendingPathComponent("\(name).png"))
                rendered += 1
            } catch { failures.append("\(name): \(error)") }
        }
        for scenario in [SingleTraderAccountFixture.zeroEquityLoss, .unavailableEquity,
                         .offlineRetainedPnl, .priorUTCDay] {
            let name = "single-trader-account-\(scenario.rawValue)"
            do {
                try render(appearance: .aqua, colorScheme: .light, size: NSSize(width: 840, height: 1100),
                           includeSnapshot: true, singleTraderFixture: true, solSampleCount: 120,
                           initialWorkspace: .singleTrader, accountFixture: scenario,
                           destination: output.appendingPathComponent("\(name).png"))
                rendered += 1
            } catch { failures.append("\(name): \(error)") }
        }
        for (name, workspace, snapshot, traderFixture, pending, paused, page) in [
            ("single-trader-offline-minimum", PrismWorkspace.singleTrader, false, false, false, false, PrismPage.overview),
            ("single-trader-chart-absent", .singleTrader, true, true, false, false, .overview),
            ("single-trader-mode-mismatch", .singleTrader, true, false, false, false, .overview),
            ("single-trader-mode-selection", .singleTrader, true, false, false, true, .overview),
            ("single-trader-eligibility-pending", .singleTrader, true, true, true, true, .overview),
            ("single-trader-pause-pending", .singleTrader, true, true, true, false, .overview),
            ("single-trader-settings", .singleTrader, true, true, false, false, .settings)
        ] {
            do {
                try render(page: page, appearance: .darkAqua, colorScheme: .dark, size: NSSize(width: 600, height: 420),
                           includeSnapshot: snapshot, singleTraderFixture: traderFixture, solSampleCount: traderFixture ? 120 : 0,
                           initialWorkspace: workspace, eligibilityPending: pending, paused: paused,
                           destination: output.appendingPathComponent("\(name).png"))
                rendered += 1
            } catch { failures.append("\(name): \(error)") }
        }
        do {
            try render(appearance: .aqua, colorScheme: .light, size: NSSize(width: 600, height: 420),
                       includeSnapshot: true, singleTraderFixture: true, solSampleCount: 120,
                       initialWorkspace: .singleTrader, tradeReviewStatus: "rejected",
                       destination: output.appendingPathComponent("single-trader-review-rejected.png"))
            rendered += 1
        } catch { failures.append("single-trader-review-rejected: \(error)") }
        print("Rendered \(rendered) screenshots in \(output.path)")
        if !failures.isEmpty {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
        let files = try FileManager.default.contentsOfDirectory(atPath: output.path).filter { $0.hasSuffix(".png") }.sorted()
        files.forEach { print(output.appendingPathComponent($0).path) }
    }

    private static func render(page: PrismPage? = nil, appearance: NSAppearance.Name, colorScheme: ColorScheme,
                               size: NSSize, includeSnapshot: Bool, singleTraderFixture: Bool = false,
                               solSampleCount: Int = 0, initialWorkspace: PrismWorkspace?,
                               eligibilityPending: Bool = false, paused: Bool = false,
                               tradeReviewStatus: String? = nil,
                               accountFixture: SingleTraderAccountFixture = .normal,
                               destination: URL) throws {
        NSApp.appearance = NSAppearance(named: appearance)
        let runtime = RuntimeClient(includeSnapshot: includeSnapshot, singleTraderFixture: singleTraderFixture,
                                    solSampleCount: solSampleCount, accountFixture: accountFixture)
        if includeSnapshot && (eligibilityPending || paused || tradeReviewStatus != nil) {
            let json = singleTraderFixture ? RuntimeClient.singleTraderFixture(solSampleCount: solSampleCount) : RuntimeClient.fixture
            var object = try JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
            if paused {
                var status = object["runtime"] as! [String: Any]
                status["status"] = "paused"
                object["runtime"] = status
            }
            if var mode = object["trading_mode"] as? [String: Any] {
                mode["pending"] = eligibilityPending
                mode["enabled"] = !paused
                if eligibilityPending { mode["unsupported_reason"] = "Synthetic fixture: qualified strategy and account approvals pending." }
                if let tradeReviewStatus {
                    mode["latest_trade_review"] = ["status": tradeReviewStatus, "decision": "reject",
                        "rationale": "Offline fixture: the proposed trade has expired.",
                        "review_id": "fixture-review", "tool_name": "submit_order_intent",
                        "created_at_ms": 1_793_347_200_000] as [String: Any]
                }
                object["trading_mode"] = mode
            }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            runtime.state = try decoder.decode(RuntimeState.self, from: JSONSerialization.data(withJSONObject: object))
        }
        if let page { runtime.selectedPage = page }
        let preferences = AppearancePreferences()
        let root = PrismHomeView(runtime: runtime, preferences: preferences, initialWorkspace: initialWorkspace)
            .frame(width: size.width, height: size.height)
            .environment(\.colorScheme, colorScheme)
        let host = NSHostingView(rootView: root)
        host.frame = NSRect(origin: .zero, size: size)
        host.wantsLayer = true
        host.layer?.backgroundColor = (colorScheme == .dark ? NSColor(calibratedWhite: 0.08, alpha: 1) : NSColor(calibratedWhite: 0.96, alpha: 1)).cgColor
        host.appearance = NSAppearance(named: appearance)
        host.layoutSubtreeIfNeeded()
        let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) ?? NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2), pixelsHigh: Int(size.height * 2),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        bitmap.size = size
        host.cacheDisplay(in: host.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "UIRenderingCheck", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not encode PNG"])
        }
        guard data.starts(with: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]) else {
            throw NSError(domain: "UIRenderingCheck", code: 2, userInfo: [NSLocalizedDescriptionKey: "Encoded screenshot has an invalid PNG signature"])
        }
        try data.write(to: destination)
    }
}
