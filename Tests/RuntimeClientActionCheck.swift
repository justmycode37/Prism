import Foundation

private actor OfflineRuntimeTransport {
    private let staleState: Data
    private var currentState: Data
    private var stateReads = 0
    private var heldRead: CheckedContinuation<RuntimeHTTPResponse, Never>?
    private var heldReadWaiter: CheckedContinuation<Void, Never>?
    private var nextControlFailure: ControlFailure?

    init(pausedState: Data, runningState: Data) {
        staleState = pausedState
        currentState = pausedState
        self.runningState = runningState
    }

    private let runningState: Data

    func send(_ request: URLRequest) async throws -> RuntimeHTTPResponse {
        guard let path = request.url?.path else { throw FixtureFailure.invalidRequest }
        if request.httpMethod == "GET" && path == "/v1/state" {
            stateReads += 1
            if stateReads == 2 {
                return await withCheckedContinuation { continuation in
                    heldRead = continuation
                    heldReadWaiter?.resume()
                    heldReadWaiter = nil
                }
            }
            return RuntimeHTTPResponse(data: currentState, statusCode: 200)
        }
        if request.httpMethod == "POST" && path == "/v1/control" {
            if let failure = nextControlFailure {
                nextControlFailure = nil
                switch failure {
                case .network:
                    throw URLError(.networkConnectionLost)
                case .unauthorized:
                    return RuntimeHTTPResponse(data: Data("{}".utf8), statusCode: 401)
                case .validation:
                    return RuntimeHTTPResponse(
                        data: Data("{\"error\":\"Start was refused by host validation.\"}".utf8),
                        statusCode: 400)
                }
            }
            guard let body = request.httpBody,
                  let payload = try JSONSerialization.jsonObject(with: body) as? [String: String],
                  payload["action"] == "start_all" else { throw FixtureFailure.invalidRequest }
            currentState = runningState
            return RuntimeHTTPResponse(data: Data("{}".utf8), statusCode: 200)
        }
        throw FixtureFailure.invalidRequest
    }

    func waitForHeldRead() async {
        if heldRead != nil { return }
        await withCheckedContinuation { continuation in heldReadWaiter = continuation }
    }

    func releaseHeldRead() {
        heldRead?.resume(returning: RuntimeHTTPResponse(data: staleState, statusCode: 200))
        heldRead = nil
    }

    func failNextControlRequest(_ failure: ControlFailure) { nextControlFailure = failure }

    func readCount() -> Int { stateReads }
}

private enum ControlFailure: Sendable {
    case network
    case unauthorized
    case validation
}

private enum FixtureFailure: Error {
    case invalidRequest
    case staleActionState
}

@MainActor
@main
enum RuntimeClientActionCheck {
    static func main() async throws {
        guard CommandLine.arguments.count == 3 else { throw FixtureFailure.invalidRequest }
        let pausedState = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let runningState = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
        let transport = OfflineRuntimeTransport(pausedState: pausedState, runningState: runningState)
        let client = RuntimeClient(requestSender: { request in try await transport.send(request) },
                                   controlToken: "offline-contract-token")

        await client.refresh()
        guard client.isConnected, client.state?.runtime.status == "paused" else {
            throw FixtureFailure.staleActionState
        }

        let overlappingRefresh = Task { await client.refresh() }
        await transport.waitForHeldRead()
        await client.control("start_all")
        guard !client.isBusy, client.state?.runtime.status == "running" else {
            throw FixtureFailure.staleActionState
        }

        // Release a response captured before Start. It must not roll the UI back
        // after the forced post-action read has observed the running state.
        await transport.releaseHeldRead()
        await overlappingRefresh.value
        guard client.state?.runtime.status == "running", await transport.readCount() == 3 else {
            throw FixtureFailure.staleActionState
        }

        let failedTransport = OfflineRuntimeTransport(pausedState: pausedState, runningState: runningState)
        let disconnectedClient = RuntimeClient(
            requestSender: { request in try await failedTransport.send(request) },
            controlToken: "offline-contract-token")
        await disconnectedClient.refresh()
        let oldStateRead = Task { await disconnectedClient.refresh() }
        await failedTransport.waitForHeldRead()
        await failedTransport.failNextControlRequest(.network)
        await disconnectedClient.control("start_all")
        guard !disconnectedClient.isConnected,
              disconnectedClient.state?.runtime.status == "paused",
              disconnectedClient.errorMessage != nil else {
            throw FixtureFailure.staleActionState
        }
        await failedTransport.releaseHeldRead()
        await oldStateRead.value
        guard !disconnectedClient.isConnected, disconnectedClient.errorMessage != nil else {
            throw FixtureFailure.staleActionState
        }

        let rejectedTransport = OfflineRuntimeTransport(pausedState: pausedState, runningState: runningState)
        let rejectedClient = RuntimeClient(
            requestSender: { request in try await rejectedTransport.send(request) },
            controlToken: "offline-contract-token")
        await rejectedClient.refresh()
        await rejectedTransport.failNextControlRequest(.unauthorized)
        await rejectedClient.control("start_all")
        guard !rejectedClient.isConnected, rejectedClient.errorMessage?.contains("token") == true else {
            throw FixtureFailure.staleActionState
        }

        let validationTransport = OfflineRuntimeTransport(pausedState: pausedState, runningState: runningState)
        let validationClient = RuntimeClient(
            requestSender: { request in try await validationTransport.send(request) },
            controlToken: "offline-contract-token")
        await validationClient.refresh()
        await validationTransport.failNextControlRequest(.validation)
        await validationClient.control("start_all")
        guard validationClient.isConnected,
              validationClient.errorMessage?.contains("host validation") == true else {
            throw FixtureFailure.staleActionState
        }
        let validationRefresh = Task { await validationClient.refresh() }
        await validationTransport.waitForHeldRead()
        await validationTransport.releaseHeldRead()
        await validationRefresh.value
        guard validationClient.isConnected,
              validationClient.errorMessage?.contains("host validation") == true else {
            throw FixtureFailure.staleActionState
        }
        await validationClient.control("start_all")
        guard validationClient.state?.runtime.status == "running", validationClient.errorMessage == nil else {
            throw FixtureFailure.staleActionState
        }
        print("PASS: mutations force fresh reads, stale requests cannot undo disconnects, and host refusals retain connectivity.")
    }
}
