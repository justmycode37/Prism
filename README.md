# Prism

Prism is a native macOS console for an autonomous research cascade. Many inexpensive agents gather evidence; progressively fewer agents analyze it, design strategies and make paper execution decisions. The existing SwiftUI interface, glass rendering, navigation and appearance controls are preserved.

The default cascade has **33 agents**:

| Layer | Agents | Model | Reasoning | Output |
| --- | ---: | --- | --- | --- |
| Collect | 24 | GPT-6 Luna | Low | Source-linked findings |
| Analyze | 6 | GPT-6.1 Sol | Medium | Ranked hypotheses with statistical context |
| Strategize | 2 | GPT-6 Astra | High | Declarative strategy candidates |
| Execute | 1 | GPT-6 Astra | Max | Decisions checked by deterministic paper execution |

Layer counts, sources and limits are configured by the backend; model and reasoning progression are fixed by layer. Crawling is bounded by configured sources, per-host pacing, robots policies, depth and request limits. It does not promise to collect the entire internet. Model claims never substitute for measured strategy validation.

## Run

The app requires macOS 26+ and Xcode 26+. The local backend uses Python 3.12+ and its standard library.

```sh
xcodebuild -project Prism.xcodeproj -scheme Prism \
  -configuration Debug -derivedDataPath build \
  CODE_SIGNING_ALLOWED=NO build
```

For a macOS login service, run:

```sh
python3 scripts/prismctl.py install
python3 scripts/prismctl.py status
python3 scripts/prismctl.py start
python3 scripts/prismctl.py stop
```

Installation links the local control token with the app's Keychain entry. The runtime reads its OpenAI credential from the configured private key file or environment. A new cascade starts **paused** in `.prism-runtime/cascade.sqlite3`; use the existing Start control when ready. The rebuilt backend does not import the previous research database or execute its strategy code.

The service continues independently of the console while the Mac is awake. Closing the console does not stop research. Execution is simulated paper trading; there is no exchange order submission or custody integration.

To change cascade settings unavailable in the existing UI, provide a partial JSON configuration:

```sh
python3 scripts/prismctl.py configure --config-file /path/to/cascade-settings.json
```

## Documentation and checks

- [Architecture](docs/Architecture.md): layer contracts, information flow and module boundaries.
- [Agents](docs/Agents.md): responsibilities, routing and the four role manuals.
- [Runtime](docs/Runtime.md): startup, configuration, crawling, controls and persistence.
- [Validation](docs/Validation.md): checks performed for this rebuild and remaining limitations.
- [Design](docs/Design.md) and [third-party components](docs/ThirdParty.md): the preserved native presentation and bundled assets.

```sh
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v
scripts/check-ui-contract.sh
```

The local glass prototype uses `PRISM_EXPERIMENTAL_GLASS` to tune private macOS rendering APIs. Remove that compilation condition to build the public `NSGlassEffectView` fallback. The prototype needs regression checks after OS updates and is not suitable for Mac App Store distribution.
