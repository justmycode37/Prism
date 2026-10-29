# Prism

Prism is a native macOS console for a paper-only research harness focused on public equities involved in AI infrastructure. It gathers public reporting, company material, market charts and other evidence, then routes that material through a fixed reverse cascade. Example symbols include MRVL, NBIS, APLD, CRWV, IREN and MU; they are discovery seeds, not an allowlist. The sector catalogue is dynamic and can discover additional qualifying public companies.

The application has **33 agents** with fixed model routing:

| Layer | Agents | Model | Reasoning |
| --- | ---: | --- | --- |
| Collect | 24 | GPT-6 Luna | Low |
| Analyze | 6 | GPT-6.1 Sol | Medium |
| Strategize | 2 | GPT-6 Astra | High |
| Execute | 1 | GPT-6 Astra | Max |

Default concurrency is 8, configurable up to 32. The runtime uses the existing OpenAI credential integration. Gemini is not a runtime provider. Agents organize evidence and hypotheses; deterministic host code owns validation, budget reservations, paper fills and accounting.

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
- [Agents](docs/Agents.md): fixed roster and responsibilities.
- [Runtime](docs/Runtime.md): catalogue, public sources and data limits.
- [Validation](docs/Validation.md): verification results and data limitations.

The local glass prototype uses `PRISM_EXPERIMENTAL_GLASS` for private macOS rendering APIs. Remove that compilation condition to use the public `NSGlassEffectView` fallback. See [Design](docs/Design.md) and [third-party components](docs/ThirdParty.md).
