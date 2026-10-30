import Foundation

struct RuntimeState: Decodable, Sendable {
    let runtime: RuntimeStatus
    let budget: RuntimeBudget
    let agents: [AgentStatus]
    let agentRoles: [AgentRole]?
    let strategies: [StrategyResult]
    let portfolio: PaperPortfolio
    let markets: [MarketQuote]
    let events: [RuntimeEvent]
    let config: RuntimeConfiguration
    /// Additive cascade metadata; nil when the backend omits the key (legacy / offline fixture).
    let cascade: CascadeMetadata?
    let research: ResearchStatus?
    /// Continuous runtime projection; absent in older journals and fixtures.
    let operations: OperationalStatus?
    /// Selected trading workflow; absent in older runtimes and offline fixtures.
    let tradingMode: TradingModeStatus?
    /// Optional bounded venue OHLC history for the dedicated SOL workspace.
    let solChart: SOLChartSnapshot?
}

struct TradingModeStatus: Decodable, Sendable {
    let selected: String
    let traderModel: String?
    let traderReasoningEffort: String?
    let supportModel: String?
    let supportReasoningEffort: String?
    let reviewerModel: String?
    let reviewerReasoningEffort: String?
    let tradeReviewRequired: Bool?
    let latestTradeReview: TradeReviewStatus?
    let symbol: String?
    let category: String?
    let primaryTimeframe: String?
    let swarmSize: Int?
    let continuous: Bool?
    let pnlBasis: String?
    let lifetimeModelBudgetUsd: Double?
    let tradingPnl: Double?
    let tradingReturn: Double?
    /// Zero is a valid observed balance; nil means no usable equity value was projected.
    let accountEquity: Double?
    let accountAsOf: String?
    let accountDay: String?
    let accountStatus: String?
    let grossExposure: Double?
    let leverage: Double?
    let leveragePolicy: String?
    let venueMaxLeverage: Double?
    let maxPositionRiskFraction: Double?
    let enabled: Bool?
    let apiSpentUsd: Double?
    let apiReservedUsd: Double?
    let pending: Bool?
    let unsupportedReason: String?
    /// Actual Bybit SOL linear position/fill state from a matching local controller snapshot.
    /// Missing in older runtimes and fixtures.
    let actualSolExecution: SOLExecutionProjection?

    var isSolPerpetual: Bool { selected == "sol_perpetual" }
}

struct SOLExecutionProjection: Decodable, Sendable {
    let environment: String?
    let positionsStatus: String?
    let positionsAsOf: String?
    let confirmedFlat: Bool?
    let positions: [SOLExecutionPosition]?
    let positionsTruncated: Bool?
    let fillsAvailable: Bool?
    let fillsTruncated: Bool?
    let fills: [SOLExecutionFill]?
}

struct SOLExecutionPosition: Decodable, Identifiable, Sendable {
    let id: String
    let category: String?
    let symbol: String?
    let side: String
    let quantity: Double
    let entryPrice: Double
    let markPrice: Double
    let leverage: Double?
    let stopLoss: Double?
    let ownership: String?
}

struct SOLExecutionFill: Decodable, Identifiable, Sendable {
    let id: String
    let side: String
    let quantity: Double
    let price: Double
    let timestamp: String
    let fee: Double?
    let feeCurrency: String?
    let realizedPnl: Double?
}

struct TradeReviewStatus: Decodable, Sendable {
    let status: String
    let decision: String?
    let rationale: String?
    let reason: String?
    let reviewId: String?
    let toolName: String?
    let createdAtMs: Int64?
}

struct OperationalStatus: Decodable, Sendable {
    let mode: String
    let enabled: Bool
    let nextWakeAt: String?
    let rationale: String?
    let riskStatus: String?
    let grossExposure: Double?
    let realizedPnl: Double?
    let unrealizedPnl: Double?
    let orderCount: Int?
    let activeOrderCount: Int?
    let ordersTruncated: Bool?
    let sections: [OperationalSection]
    let componentCosts: [OperationalCost]

    func section(id: String) -> OperationalSection? {
        sections.first { $0.id == id }
    }
}

struct OperationalSection: Decodable, Identifiable, Sendable {
    let id: String
    let title: String
    let items: [OperationalItem]
}

struct OperationalItem: Decodable, Identifiable, Sendable {
    let id: String
    let title: String
    let detail: String
    let status: String?
    let time: String?
    let sourceUrl: String?
}

struct OperationalCost: Decodable, Identifiable, Sendable {
    let id: String
    let calls: Int
    let spentUsd: Double
    let reservedUsd: Double
    let limitUsd: Double
}

struct ResearchStatus: Decodable, Sendable {
    let phase: String?
    let historicalResultsAreDevelopment: Bool?
    let experimentCount: Int?
    let statuses: [String: Int]?
    let failureReasons: [String: Int]?
    let operatingCosts: ResearchCosts?
    let coverage: ResearchCoverage?
    let forwardReports: [ForwardResearchReport]?
    /// Optional bounded projection; older runtimes do not send it.
    let leaderboard: ResearchLeaderboard?
}

struct ResearchLeaderboard: Decodable, Sendable {
    let rows: [ResearchLeaderboardRow]?
    let total: Int?
    let ranked: Int?
    let rankingBasis: String?
    let limit: Int?

    var testWindows: [String] {
        Array(Set((rows ?? []).compactMap(\.testWindow))).sorted(by: >)
    }

    /// Sort finite development returns first, preserving backend order for ties
    /// and for rows whose return has not been measured.
    var rankedRows: [ResearchLeaderboardRow] {
        (rows ?? []).enumerated().sorted { lhs, rhs in
            let left = lhs.element.finiteNetReturn
            let right = rhs.element.finiteNetReturn
            switch (left, right) {
            case let (left?, right?):
                return left == right ? lhs.offset < rhs.offset : left > right
            case (_?, nil):
                return true
            case (nil, _?):
                return false
            case (nil, nil):
                return lhs.offset < rhs.offset
            }
        }.map(\.element)
    }
}

struct ResearchLeaderboardRow: Decodable, Identifiable, Sendable {
    let id: String
    let experimentId: String?
    let name: String?
    let symbol: String?
    let family: String?
    let phase: String?
    let netReturn: Double?
    let maxDrawdown: Double?
    let trades: Int?
    let screenStatus: String?
    let prospectiveStatus: String?
    let qualification: ResearchQualification?
    let forwardNetReturn: Double?
    let forwardAsOf: String?
    let testStart: String?
    let testEnd: String?

    var testWindow: String? {
        guard let testStart, let testEnd, !testStart.isEmpty, !testEnd.isEmpty else { return nil }
        return "\(testStart) → \(testEnd)"
    }

    var finiteNetReturn: Double? {
        guard let netReturn, netReturn.isFinite else { return nil }
        return netReturn
    }

    var finiteMaxDrawdown: Double? {
        guard let maxDrawdown, maxDrawdown.isFinite else { return nil }
        return maxDrawdown
    }
}

struct ResearchQualification: Decodable, Sendable {
    let qualified: Bool?
    let status: String?
    let basis: String?
    let rankingEffect: String?
    let reasons: [String]?
}

struct ResearchCosts: Decodable, Sendable {
    let modelCostUsd: Double?
    let totalCostUsd: Double?
    let calls: Int?
}

struct ResearchCoverage: Decodable, Sendable {
    let gaps: [String]?
    let survivorshipSafe: Bool?
}

struct ForwardResearchReport: Decodable, Identifiable, Sendable {
    var id: String { experimentId }
    let experimentId: String
    let status: String?
    let sessions: Int?
    let netReturn: Double?
    let excessReturn: Double?
    let operatingPnlUsd: Double?
    let reasons: [String]?
}

// MARK: - Cascade metadata (additive; all fields optional for backward compatibility)

struct CascadeMetadata: Decodable, Sendable {
    let layers: [CascadeLayer]?
    /// Discovered sector universe. Nil when the backend has not yet run sector discovery.
    let sector: CascadeSector?
    /// Evidence-collection counters. Nil when no collection cycle has completed.
    let collection: CascadeCollection?
}

struct CascadeLayer: Decodable, Sendable {
    let id: String
    let name: String?
    let count: Int
    let reasoningEffort: String
    let model: String?
}

/// A discovered sector: its identity, constituent companies, and discovery provenance.
struct CascadeSector: Decodable, Sendable {
    let id: String
    let name: String
    let companies: [CascadeCompany]
    /// Ticker symbols for quick lookup (may overlap with companies[].symbol).
    let symbols: [String]
    /// "complete", "partial", "pending", or nil when unknown.
    let discoveryStatus: String?
}

/// A single equity discovered within a sector.
struct CascadeCompany: Decodable, Identifiable, Sendable {
    let symbol: String
    let name: String
    let themes: [String]
    /// Primary source URLs that surfaced this company.
    let sourceUrls: [String]
    /// Evidence document IDs supporting the inclusion decision.
    let evidenceIds: [String]
    /// Human-readable explanation of why this company was included.
    let discoveryReason: String

    var id: String { symbol }
}

/// Aggregate counters from the most recent evidence-collection cycle.
struct CascadeCollection: Decodable, Sendable {
    let evidenceCount: Int
    let sourceCount: Int
    let collectorCount: Int
    let activeCollectorCount: Int?
}

struct RuntimeStatus: Decodable, Sendable {
    let status: String
    let mode: String
    let startedAt: String?
    let nextCycleAt: String?
    let openaiConfigured: Bool
    let lastError: String?
    /// Whether any provider API key is ready for inference. Nil when the backend does not report this field.
    let credentialsReady: Bool?
}

struct RuntimeBudget: Decodable, Sendable {
    let dailyLimitUsd: Double?
    let spentTodayUsd: Double
    let reservedUsd: Double
    let callsToday: Int
    let maxCallsPerDay: Int?
}

struct AgentStatus: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let team: String
    let status: String
    let task: String
    let lastRunAt: String?
    let summary: String
    let roleId: String?
    let parentId: String?
    let kind: String?
    let mandate: String?
    let createdBy: String?
    let createdAt: String?
    let creationReason: String?
    let instructionPath: String?
    let instructions: String?
    let isDynamic: Bool?
    let model: String?
    let reasoningEffort: String?
    let capabilities: [AgentCapability]?
    let recentTools: [AgentToolActivity]?

    var isOrchestrator: Bool { kind == "orchestrator" }
}

struct AgentCapability: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let description: String
    let available: Bool
    let reason: String?
}

struct AgentToolActivity: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let status: String
    let summary: String
    let createdAt: String
    let durationMs: Int
}

struct AgentRole: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let team: String
    let kind: String
    let researcher: Bool
    let mandate: String
    let templateRoleId: String?
    let instructionPath: String?
    let instructions: String?
    let isDynamic: Bool?
    let createdBy: String?
    let createdAt: String?
}

struct StrategyResult: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let symbol: String
    let family: String
    let status: String
    let netReturn: Double?
    let sharpe: Double?
    let maxDrawdown: Double?
    let trades: Int?
    let reason: String
    let createdAt: String
    let agentId: String?
    let spec: StrategySpecification?
    let phase: String?
    let experimentId: String?
}

struct StrategySpecification: Decodable, Sendable {
    let symbol: String
    let kind: String
    let parameters: [String: Double]
    let sourceSha256: String?

    private enum CodingKeys: String, CodingKey {
        case symbol, kind, parameters
    }

    private struct PythonParameters: Decodable {
        let sourceSha256: String
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        symbol = try container.decode(String.self, forKey: .symbol)
        kind = try container.decode(String.self, forKey: .kind)
        if kind == "python" {
            sourceSha256 = try container.decode(PythonParameters.self, forKey: .parameters).sourceSha256
            parameters = [:]
        } else {
            parameters = try container.decode([String: Double].self, forKey: .parameters)
            sourceSha256 = nil
        }
    }
}

struct PaperPortfolio: Decodable, Sendable {
    let equity: Double
    let cash: Double
    let realizedPnl: Double
    let unrealizedPnl: Double
    let initialEquity: Double?
    let asOf: String?
    let pnlHistory: [PaperPnlObservation]?
    let positions: [PaperPosition]
    let trades: [PaperTrade]
    let valuationComplete: Bool?
    let riskHalted: Bool?
    let warnings: [String]?
    let receivables: [IncomeReceivable]?

    /// Equity includes positions, simulated trading costs, and observed income entitlements.
    var totalPnl: Double? {
        guard let initialEquity, initialEquity.isFinite, initialEquity > 0, equity.isFinite else { return nil }
        let result = equity - initialEquity
        return result.isFinite ? result : nil
    }

    var totalReturn: Double? {
        guard let totalPnl, let initialEquity else { return nil }
        let result = totalPnl / initialEquity
        return result.isFinite ? result : nil
    }

    var pnlSamples: [PaperPnlSample] {
        guard let pnlHistory, !pnlHistory.isEmpty else {
            // Older runtimes have only a current valuation. Never invent its past.
            guard let date = PrismFormat.date(asOf), let totalPnl else { return [] }
            return [PaperPnlSample(id: 0, date: date, pnl: totalPnl)]
        }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let wholeSeconds = ISO8601DateFormatter()
        return pnlHistory.enumerated().compactMap { index, observation -> PaperPnlSample? in
            guard observation.pnl.isFinite,
                  let date = fractional.date(from: observation.timestamp) ?? wholeSeconds.date(from: observation.timestamp) else {
                return nil
            }
            return PaperPnlSample(id: index, date: date, pnl: observation.pnl)
        }.sorted {
            // Keep all changes at a shared quote time in their recorded order.
            $0.date == $1.date ? $0.id < $1.id : $0.date < $1.date
        }
    }
}

struct IncomeReceivable: Decodable, Identifiable, Sendable {
    let id: String
    let amount: Double
    let payableAtMs: Double?
}

struct PaperPnlObservation: Decodable, Equatable, Sendable {
    let timestamp: String
    let pnl: Double
}

struct PaperPnlSample: Identifiable, Equatable, Sendable {
    let id: Int
    let date: Date
    let pnl: Double
}

struct PaperPosition: Decodable, Identifiable, Sendable {
    var id: String { "\(symbol)-\(side)" }
    let symbol: String
    let side: String
    let quantity: Double
    let entryPrice: Double
    let markPrice: Double
    let unrealizedPnl: Double
}

struct PaperTrade: Decodable, Identifiable, Sendable {
    let id: String
    let symbol: String
    let side: String
    let quantity: Double
    let price: Double
    let timestamp: String
    let realizedPnl: Double?
}

struct MarketQuote: Decodable, Identifiable, Sendable {
    var id: String { symbol }
    let symbol: String
    let price: Double
    let change24h: Double?
    let fundingRate: Double?
    let updatedAt: String

    func freshness(at now: Date, staleAfter: TimeInterval = 15,
                   futureTolerance: TimeInterval = 5) -> MarketQuoteFreshness {
        guard let date = PrismFormat.date(updatedAt) else { return .unavailable }
        let age = now.timeIntervalSince(date)
        guard age.isFinite else { return .unavailable }
        if age < -futureTolerance { return .clockSkew }
        return age > staleAfter ? .stale(age: age) : .current(age: max(0, age))
    }

    // Foundation capitalizes the letter after the numeric component in change_24h.
    private enum CodingKeys: String, CodingKey {
        case symbol, price, fundingRate, updatedAt
        case change24h = "change24H"
    }
}

enum MarketQuoteFreshness: Equatable, Sendable {
    case current(age: TimeInterval)
    case stale(age: TimeInterval)
    case clockSkew
    case unavailable
}

struct RuntimeEvent: Decodable, Identifiable, Sendable {
    let id: Int
    let timestamp: String
    let level: String
    let source: String
    let message: String
}

struct RuntimeConfiguration: Codable, Sendable {
    var model: String
    var maxConcurrency: Int
    var cycleIntervalSeconds: Int
    var dailyLimitUsd: Double?
    var maxCallsPerDay: Int?

    private enum CodingKeys: String, CodingKey {
        case model, maxConcurrency, cycleIntervalSeconds, dailyLimitUsd, maxCallsPerDay
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(model, forKey: .model)
        try container.encode(maxConcurrency, forKey: .maxConcurrency)
        try container.encode(cycleIntervalSeconds, forKey: .cycleIntervalSeconds)
        if let dailyLimitUsd {
            try container.encode(dailyLimitUsd, forKey: .dailyLimitUsd)
        } else {
            // Omitting this key would preserve an existing ceiling on the server.
            try container.encodeNil(forKey: .dailyLimitUsd)
        }
        if let maxCallsPerDay {
            try container.encode(maxCallsPerDay, forKey: .maxCallsPerDay)
        } else {
            try container.encodeNil(forKey: .maxCallsPerDay)
        }
    }
}

enum PrismPage: String, CaseIterable, Identifiable {
    case overview, research, test, execution, agents, settings
    static let mainSections: [PrismPage] = [.overview, .research, .test, .execution, .agents]
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .overview: "chart.xyaxis.line"
        case .research: "scope"
        case .test: "checkmark.seal"
        case .execution: "arrow.triangle.swap"
        case .agents: "person.2"
        case .settings: "slider.horizontal.3"
        }
    }
}

enum PrismFormat {
    static func money(_ amount: Double, decimals: Int = 2) -> String {
        amount.formatted(.currency(code: "USD").precision(.fractionLength(decimals)))
    }
    static func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.formatted(.percent.precision(.fractionLength(2)))
    }
    static func number(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.formatted(.number.precision(.fractionLength(2)))
    }
    static func date(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
    static func time(_ value: String?) -> String {
        guard let date = date(value) else { return "Not yet" }
        return date.formatted(date: .omitted, time: .shortened)
    }
    static func label(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }
}
