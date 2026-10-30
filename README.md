# Prism

Prism is a native macOS console and a Python research runtime with two lanes: the existing paper-only daily-equity v1 research protocol and an event-driven Bybit v2 research and execution protocol. Bybit mainnet and live execution are disabled by default. Historical measurements remain development evidence; a leaderboard or backtest result does not establish profitability or grant order authority.

## Start here

- [Documentation index](docs/README.md) separates operator guidance from architecture and research methods.
- [Research workflow](docs/ResearchWorkflow.md) explains source review, hypothesis variation, screening, and how to read the development leaderboard.
- [Architecture](docs/Architecture.md) describes the implemented data flow and layer boundaries.
- [Research contract](docs/ResearchContract.md) defines experiment identity, statistical gates, and prospective evidence.
- [Agent procedures](docs/Agents.md) links the executable model manuals and their actual request limits.
- [Runtime guide](docs/Runtime.md) covers installation, controls, configuration, data, and storage.
- [Continuous runtime](docs/ContinuousRuntime.md) describes independent workers, source-backed scheduling, durable research dossiers, Bybit identity and operator gates, and offline operating-cost estimates.
- [SOL Perpetual mode](docs/SOLPerpetualMode.md) explains the in-app Research/SOL mode selector, the one-minute Sol xhigh trader, Luna max support swarm, and agent-selected venue-verified leverage.
- [Operational cost fixture report](docs/OperationalCosts.json) records request sizes, token assumptions, lane caps, and workload scenarios without live model calls.

## Runtime

The app requires macOS 26+ and Xcode 26+. The local backend uses Python 3.12+ and its standard library. New installs and process restarts start paused; closing the console does not stop the installed service. Use the [runtime guide](docs/Runtime.md) and [continuous-runtime guide](docs/ContinuousRuntime.md) for operator commands and controls.

```sh
xcodebuild -project Prism.xcodeproj -scheme Prism -configuration Debug -derivedDataPath build CODE_SIGNING_ALLOWED=NO build
```

## Offline dataset research

The research CLI uses an explicit versioned JSON dataset and a temporary SQLite journal. It does not need the installed service, model credentials, or network access. `search` runs a bounded deterministic grid; see the [workflow guide](docs/ResearchWorkflow.md) for a complete example and interpretation limits.

```sh
python3 scripts/research.py search --journal /tmp/prism-search.sqlite3 \
  --dataset /path/to/offline-dataset-v1.json --max-candidates 256 \
  --families momentum mean_reversion breakout event_surprise \
  --lookbacks 3 5 8 10 15 20 30 40 60 80 100 \
  --output /tmp/prism-search-report.json
```

Dataset requirements and provider-declared provenance are covered in [Runtime](docs/Runtime.md). A synthetic or vendor-declared input remains development evidence; it is not independently verified by import.

## Continuous operations

The event-driven runtime reacts to market boundaries, account events, sourced calendar records, review conditions, and freshness changes. It includes paper Bybit execution and a real-order adapter, but live execution and mainnet both remain disabled until an operator configures the exact instrument and version allowlists, the full risk envelope, a private mode-specific credential, and the mainnet opt-in. Cash Spot uses verified exchange conditional stops; linear perpetuals use verified exchange position stops. See [Continuous runtime](docs/ContinuousRuntime.md) before changing those controls. The safe disabled configuration is [examples/operator-config.json](examples/operator-config.json); [examples/calendar.json](examples/calendar.json) is an expired synthetic schema fixture.

## Development checks

```sh
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v
scripts/check-ui-contract.sh
```

These checks use local fixtures. They do not start the installed service. See [Validation](docs/Validation.md) for known limits and [Profitability readiness](docs/ProfitabilityReadiness.md) for the recorded readiness assessment.

## Design notes

The glass prototype uses `PRISM_EXPERIMENTAL_GLASS` for private macOS rendering APIs. Remove that compilation condition to use the public `NSGlassEffectView` fallback. See [Design](docs/Design.md) and [third-party components](docs/ThirdParty.md).
