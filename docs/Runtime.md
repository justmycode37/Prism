# Runtime

The backend is a fresh reverse-cascade implementation behind the existing native console API. It uses Python 3.12+ and the standard library. No paid provider calls, service installation or real trading are required to run its automated checks.

## Startup and persistence

```sh
python3 scripts/prismctl.py install
python3 scripts/prismctl.py status
```

The macOS installer creates or reuses `.prism-runtime/control.token`, links it to the console's `studio.prism.control` Keychain entry and installs `studio.prism.runtime` as a per-user login service. It preserves an existing nonempty token. The new SQLite database is `.prism-runtime/cascade.sqlite3`; every process start begins paused, including service restarts. The previous database is not loaded into the rebuilt runtime.

For direct launch, set `PYTHONPATH=backend` and run `python3 -m prism_runtime` after providing the private control-token file. Environment variables `PRISM_STATE_DIR` and `PRISM_CONTROL_TOKEN_FILE` select alternative locations. The launcher acquires a process lock, binds loopback HTTP, runs the scheduler and drains it on shutdown.

The OpenAI credential stays outside the database and API state. `PRISM_OPENAI_KEY_FILE` selects its private file; the runtime can also use its supported environment credential. The console keeps its existing Keychain control-token connection flow. Do not put credentials in source configuration or collection URLs.

## Controls

The existing Start/Stop control starts or pauses the cascade. CLI controls are also available:

```sh
python3 scripts/prismctl.py start
python3 scripts/prismctl.py stop
python3 scripts/prismctl.py pause
python3 scripts/prismctl.py resume
python3 scripts/prismctl.py run
```

`run` requests an immediate cycle and leaves continuous scheduling enabled; use `pause` or `stop` to suspend it. State, persisted configuration, roster progress, strategy outcomes, budget and events are exposed through the authenticated local API. Closing the app does not stop the separately installed process. The host must remain awake and connected for continuous work.

## Configuration

The backend stores a validated configuration and accepts partial updates through `POST /v1/config`. The unchanged native Settings form sends its existing model, concurrency, interval and optional budget fields. Additional cascade fields can be configured with a JSON file:

```sh
python3 scripts/prismctl.py configure --config-file /path/to/cascade-settings.json
```

Defaults route 24 collectors to Luna with low reasoning, 6 analysts to Sol with medium reasoning, 2 strategists to Astra with high reasoning and 1 executor to Astra with max reasoning. A configured credential is required for provider work. Optional daily spending and call ceilings can be enabled; `null` disables a ceiling while retaining usage accounting. Spending uses conservative configured per-model reservation rates and reported token usage; it is a local estimate, not a provider invoice. A request with an unknown remote outcome consumes its reservation instead of being reported as free.

## Collection and validation

Default news sources are the Federal Reserve, ECB, Ethereum Foundation, Bitcoin Core and Glassnode feeds. These sources were checked with read-only fetches during the rebuild; each can still change or become unavailable. Source failures are isolated; a blocked or changed feed does not become successful coverage. A source can be an RSS URL string or an object with `url`, `kind` (`rss`, `atom` or `article`), optional `name` and optional `keywords`.

For example, this partial configuration selects one feed and one HTML crawl root:

```json
{
  "sources": [
    {"url": "https://www.federalreserve.gov/feeds/press_all.xml", "kind": "rss", "name": "Federal Reserve"},
    {"url": "https://blog.ethereum.org/", "kind": "article", "name": "Ethereum blog"}
  ],
  "ingestion": {"max_pages": 16, "max_requests": 48, "crawl_depth": 1}
}
```

The default per-collector collection budget is 30 seconds, 48 requests and 16 pages. It reads up to four entries and one linked article per feed, follows HTML links on the same host to depth one, and caps each response at 1 MB. All collectors share fetch concurrency, caches and per-host pacing, with a minimum half-second default host delay or a longer robots delay. DNS results must be public; connections are pinned to validated addresses and redirects are rechecked. Proxies are not used.

The source layer retrieves configured public web pages, feeds, news and market observations within bounded request, depth, size and pacing limits. Robots checks and public-network restrictions apply to crawl targets. Source records retain URLs, times and content identity so downstream decisions can be traced.

Collector batches feed analysts, then strategists. Quotes and portfolio risk checks refresh on a separate 60-second timer during active research; the default research cadence is 15 minutes after a completed cycle. Model outputs must pass host parsing and provenance checks. Strategies are declarative specifications; the backend computes validation statistics and the paper engine owns execution. Unmeasured or rejected proposals retain absent or negative metrics in the existing Test screen.

The new backend contains no arbitrary Python strategy authoring or execution workbench. Public Bybit spot quotes and closed hourly candles provide reference market observations. Paper orders use long/flat cash accounting with no borrowing or funding charges; fills are simulated. Timestamped observations, validated candidates and deterministic portfolio constraints govern each paper action. There is no real exchange-order integration.

## Local API

The listener is `127.0.0.1:8787`. Its existing `/v1/state`, `/v1/control` and `/v1/config` interfaces remain compatible with the native console. Requests require the private bearer token. The new `cascade` state object adds diagnostic layer/cycle information without changing SwiftUI rendering.

To remove the login service while keeping local data and credentials:

```sh
python3 scripts/prismctl.py uninstall
```

The current fetcher reads RSS, Atom and ordinary HTML. It does not render JavaScript or extract PDFs. An unsupported linked document retains its available feed summary and records the fetch gap.
