import Combine
import Foundation
import LocalAuthentication
import Security

struct RuntimeHTTPResponse: Sendable {
    let data: Data
    let statusCode: Int
}

typealias RuntimeRequestSender = @Sendable (URLRequest) async throws -> RuntimeHTTPResponse

@MainActor
final class RuntimeClient: ObservableObject {
    @Published private(set) var state: RuntimeState?
    @Published private(set) var pnlSamples: [PaperPnlSample] = []
    @Published private(set) var isConnected = false
    @Published private(set) var isBusy = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var lastUpdated: Date?
    @Published var selectedPage: PrismPage = .overview

    private let endpoint = "http://127.0.0.1:8787"
    private var token: String
    private var hasToken = false
    private let requestSender: RuntimeRequestSender
    private var isFetching = false
    private var stateFetchGeneration: UInt64 = 0
    private var mutationFeedback: String?

    init(requestSender: RuntimeRequestSender? = nil, controlToken: String? = nil) {
        token = controlToken ?? ""
        hasToken = !(controlToken ?? "").isEmpty
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 8
        configuration.timeoutIntervalForResource = 12
        configuration.waitsForConnectivity = false
        let urlSession = URLSession(configuration: configuration, delegate: RuntimeRedirectPolicy(), delegateQueue: nil)
        self.requestSender = requestSender ?? { request in
            let (data, response) = try await urlSession.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw RuntimeError.message("The runtime returned an invalid response.")
            }
            return RuntimeHTTPResponse(data: data, statusCode: http.statusCode)
        }
    }

    func monitor() async {
        while !Task.isCancelled {
            await refresh()
            do { try await Task.sleep(for: .seconds(3)) }
            catch { break }
        }
    }

    func refresh() async {
        guard !isFetching else { return }
        isFetching = true
        defer { isFetching = false }
        if !hasToken {
            if let provisioned = await Task.detached(priority: .userInitiated, operation: { ControlTokenStore.read() }).value, !provisioned.isEmpty {
                token = provisioned
                hasToken = true
            }
        }
        guard hasToken else {
            isConnected = false
            errorMessage = "Run `python3 scripts/prismctl.py install` from the Prism project folder to connect."
            return
        }
        await fetchState()
    }

    private func fetchState() async {
        stateFetchGeneration &+= 1
        let generation = stateFetchGeneration
        do {
            let data = try await send(path: "/v1/state")
            guard !Task.isCancelled, generation == stateFetchGeneration else { return }
            let previousPortfolio = state?.portfolio
            let previousSamples = pnlSamples
            let (value, samples) = try await Task.detached(priority: .utility) {
                let decoder = JSONDecoder()
                decoder.keyDecodingStrategy = .convertFromSnakeCase
                let value = try decoder.decode(RuntimeState.self, from: data)
                let portfolio = value.portfolio
                let unchanged = portfolio.pnlHistory == previousPortfolio?.pnlHistory
                    && portfolio.asOf == previousPortfolio?.asOf
                    && portfolio.totalPnl == previousPortfolio?.totalPnl
                // History is unbounded. Reuse unchanged samples, and prepare new
                // timestamps away from the UI thread when a valuation arrives.
                return (value, unchanged ? previousSamples : portfolio.pnlSamples)
            }.value
            guard !Task.isCancelled, generation == stateFetchGeneration else { return }
            pnlSamples = samples
            state = value
            isConnected = true
            lastUpdated = .now
            errorMessage = mutationFeedback
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled, generation == stateFetchGeneration else { return }
            isConnected = false
            errorMessage = readable(error)
        }
    }

    func control(_ action: String) async {
        guard !isBusy, isConnected else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["action": action])
            _ = try await send(path: "/v1/control", method: "POST", body: body)
            // Force a post-mutation read even when the monitor already has a
            // request in flight. The generation check prevents that older
            // response from replacing this confirmed state afterward.
            await refreshAfterMutation()
        } catch { reportMutationFailure(error) }
    }

    func updateConfiguration(_ configuration: RuntimeConfiguration) async -> Bool {
        guard !isBusy, isConnected else { return false }
        isBusy = true
        defer { isBusy = false }
        do {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            _ = try await send(path: "/v1/config", method: "POST", body: encoder.encode(configuration))
            await refreshAfterMutation()
            return true
        } catch {
            reportMutationFailure(error)
            return false
        }
    }

    /// Sends only the mode selection. Encoding the whole local configuration here
    /// could erase fields owned by a newer runtime.
    func selectTradingMode(_ mode: String) async -> Bool {
        guard ["research", "sol_perpetual"].contains(mode), !isBusy, isConnected else { return false }
        isBusy = true
        defer { isBusy = false }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["trading_mode": ["selected": mode]])
            _ = try await send(path: "/v1/config", method: "POST", body: body)
            await refreshAfterMutation()
            return true
        } catch {
            reportMutationFailure(error)
            return false
        }
    }

    func updateDailySpendCeiling(_ amount: Double?) async -> Bool {
        guard !isBusy, isConnected else { return false }
        if let amount, !amount.isFinite || amount <= 0 {
            reportMutationFailure(RuntimeError.message("Enter a USD amount greater than zero."))
            return false
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let value: Any = amount.map { $0 as Any } ?? NSNull()
            let body = try JSONSerialization.data(withJSONObject: ["daily_limit_usd": value])
            _ = try await send(path: "/v1/config", method: "POST", body: body)
            await refreshAfterMutation()
            return true
        } catch {
            reportMutationFailure(error)
            return false
        }
    }

    private func validatedURL(_ value: String) throws -> URL {
        guard let url = URL(string: value), let host = url.host,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else {
            throw RuntimeError.message("Enter a runtime origin, such as http://127.0.0.1:8787.")
        }
        let local = ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host.lowercased())
        guard url.scheme == "https" || (url.scheme == "http" && local) else {
            throw RuntimeError.message("Remote runtimes need HTTPS. HTTP is allowed only on this Mac.")
        }
        return url
    }

    private func send(path: String, method: String = "GET", body: Data? = nil) async throws -> Data {
        let base = try validatedURL(endpoint)
        guard let url = URL(string: path, relativeTo: base) else { throw RuntimeError.message("Invalid runtime address.") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let response = try await requestSender(request)
        guard (200..<300).contains(response.statusCode) else {
            if response.statusCode == 401 || response.statusCode == 403 {
                throw RuntimeError.authentication("The local control token was rejected. Reinstall the Prism service to relink it.")
            }
            if let value = try? JSONSerialization.jsonObject(with: response.data) as? [String: Any],
               let message = value["error"] as? String {
                throw RuntimeError.message(message)
            }
            throw RuntimeError.message("Runtime request failed (HTTP \(response.statusCode)).")
        }
        return response.data
    }

    private func readable(_ error: Error) -> String {
        if let error = error as? RuntimeError { return error.localizedDescription }
        if error is DecodingError { return "The runtime response does not match this app version." }
        if let error = error as? URLError {
            if error.code == .cannotConnectToHost || error.code == .networkConnectionLost || error.code == .timedOut {
                return "Runtime unavailable. Check that the Prism service is running."
            }
        }
        return error.localizedDescription
    }

    private func reportMutationFailure(_ error: Error) {
        // A failed local transport means the last projected status is only an
        // offline snapshot. Keep it visible, but disable further operator actions.
        let authenticationRejected = (error as? RuntimeError)?.invalidatesConnection == true
        if error is URLError || authenticationRejected {
            mutationFeedback = nil
            stateFetchGeneration &+= 1
            isConnected = false
        } else {
            // Monitoring does not acknowledge an operator refusal. Keep its
            // explanation visible until a subsequent mutation succeeds.
            mutationFeedback = readable(error)
        }
        errorMessage = readable(error)
    }

    private func refreshAfterMutation() async {
        mutationFeedback = nil
        await fetchState()
    }
}

private enum RuntimeError: LocalizedError {
    case message(String)
    case authentication(String)

    var invalidatesConnection: Bool {
        if case .authentication = self { true } else { false }
    }

    var errorDescription: String? {
        switch self {
        case let .message(message), let .authentication(message): message
        }
    }
}

private final class RuntimeRedirectPolicy: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        // Control credentials must never be forwarded to a redirected origin.
        completionHandler(nil)
    }
}

private enum ControlTokenStore {
    static let service = "studio.prism.control"
    static let accounts = ["local", "mac-app"]
    static func query(account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service, kSecAttrAccount as String: account]
    }

    static func read() -> String? {
        for account in accounts {
            var request = query(account: account)
            request[kSecReturnData as String] = true
            let context = LAContext()
            context.interactionNotAllowed = true
            request[kSecUseAuthenticationContext as String] = context
            request[kSecMatchLimit as String] = kSecMatchLimitOne
            var result: CFTypeRef?
            if SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
               let data = result as? Data,
               let value = String(data: data, encoding: .utf8), !value.isEmpty {
                return value
            }
        }
        return nil
    }
}
