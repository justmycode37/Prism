# Prism

Prism is a native macOS console for a paper-only research harness focused on public equities involved in AI infrastructure. It gathers public reporting, company material, market charts and other evidence, then routes that material through a fixed reverse cascade. Example symbols include MRVL, NBIS, APLD, CRWV, IREN and MU; they are discovery seeds, not an allowlist. The sector catalogue is dynamic and can discover additional qualifying public companies.

The application has **33 agents** with fixed model routing:

| Layer | Agents | Model | Reasoning |
| --- | ---: | --- | --- |
| Collect | 24 | GPT-6 Luna | Low |
| Analyze | 6 | GPT-6.1 Sol | Medium |
| Strategize | 2 | GPT-6 Astra | High |
| Execute | 1 | GPT-6 Astra | Max |

The table describes available roles. Defaults activate six collectors and two analysts, with deterministic candidate selection and execution authorization. Optional strategist/executor calls are advisory comparisons. Unchanged evidence and daily histories skip repeated model work. Default concurrency is 8, configurable up to 32. The runtime uses the existing OpenAI credential integration; Gemini is not a runtime provider.

Historical results are **development evidence**. Candidates are registered before evaluation and run in independent prospective shadow accounts. Promotion to the aggregate paper portfolio requires future observations, operating returns, stock/market/sector comparisons, and predeclared statistical reviews. A shared daily engine owns replay and forward orders, costs, exposure budgets, corporate actions and accounting. Current public data does not establish profitability or verified historical execution coverage.

## Run

The app requires macOS 26+ and Xcode 26+. The local backend uses Python 3.12+ and its standard library.

```sh
xcodebuild -project Prism.xcodeproj -scheme Prism -configuration Debug -derivedDataPath build CODE_SIGNING_ALLOWED=NO build
```

Install and control the local background service:

```sh
python3 scripts/prismctl.py install
python3 scripts/prismctl.py status
python3 scripts/prismctl.py start
python3 scripts/prismctl.py stop
```

The runtime starts paused in `.prism-runtime/cascade.sqlite3`. Its OpenAI credential comes from `PRISM_OPENAI_KEY_FILE`, `OPENAI_API_KEY`, or the existing private runtime credential/Keychain entry. Closing the console does not stop the installed service. Read [Runtime](docs/Runtime.md) for configuration and controls.

```sh
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v
scripts/check-ui-contract.sh
```

## Documentation

- [Architecture](docs/Architecture.md): evidence flow and layer boundaries.
- [Task procedures](docs/Agents.md): evidence checks, output contracts and abstention criteria for each stage.
- [Runtime](docs/Runtime.md): catalogue, public sources and data limits.
- [Validation](docs/Validation.md): verification results and data limitations.
- [Research contract](docs/ResearchContract.md): frozen experiments, execution and promotion rules.
- [Readiness assessment](docs/ProfitabilityReadiness.md): original findings and implementation follow-up.

Evaluate a versioned vendor dataset without credentials or a running service:

```sh
python3 scripts/research.py evaluate --journal /tmp/prism-research.sqlite3 \
  --dataset /path/to/dataset.json --proposals /path/to/proposals.json --output /tmp/report.json
```

Dataset requirements and the distinction between provider declarations and independently verified data are described in [Runtime](docs/Runtime.md).

The local glass prototype uses `PRISM_EXPERIMENTAL_GLASS` for private macOS rendering APIs. Remove that compilation condition to use the public `NSGlassEffectView` fallback. See [Design](docs/Design.md) and [third-party components](docs/ThirdParty.md).
