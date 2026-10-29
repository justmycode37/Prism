# Runtime

Prism runs a standard-library Python service behind the existing SwiftUI console. The research scope is public AI infrastructure equities. Model work is routed through the existing OpenAI credential integration; no Gemini runtime is present. The harness is paper-only.

## Roster and lifecycle

The fixed 33-agent roster is 24 GPT-6 Luna low-reasoning collectors, 6 GPT-6.1 Sol medium-reasoning analysts, 2 GPT-6 Astra high-reasoning strategists and 1 GPT-6 Astra max-reasoning executor. Default concurrency is 8, configurable up to 32. Each cycle reserves budget before provider calls. Reservations are reconciled against reported usage; uncertain remote outcomes retain their reservation. Pause and stop checkpoints must be honored between bounded units of work, including before downstream handoffs and paper actions. Starts begin paused.

## Sector catalogue

The configured sector record uses this shape:

```json
{
  "id": "ai_infrastructure",
  "max_symbols": 80,
  "discovery_enabled": true,
  "seed_symbols": ["MRVL", "NBIS", "APLD", "CRWV", "IREN", "MU"],
  "keywords": ["AI infrastructure", "data center", "accelerator", "memory", "power", "networking"]
}
```

Seeds are examples and fallback candidates, not a symbol whitelist. Discovery can add qualifying public equities up to 80 symbols. Record discovery results, source provenance, exclusions and any seed fallback so operators can tell whether discovery succeeded or fell back.

## Public collection

Ingestion bounds are 48 pages, 160 requests, 90 seconds per bounded collection operation, crawl depth 2, 1 MB per response, 24,000 extracted content characters, 256 shared evidence items and 1,260 daily candles. The cycle-wide evidence handoff budget is 4,000. A collector may produce a large packet; shard article bodies, feed items and chart series into provenance-preserving records and enforce aggregate limits explicitly.

Fetch only public data with Python standard-library networking. Do not use model calls, shell commands, browsers or credentials for acquisition. Enforce HTTPS where applicable, public-address DNS checks, address pinning, redirect revalidation, robots.txt, bounded host pacing, shared caches and per-source failure reporting. Cache freshness and failures should be observable. A failure, robots block, timeout, or truncated response must never be reported as successful coverage. Extracted article bodies are retained up to the configured content cap. Full acquired chart series stay in the research journal; collector packets include the latest 256 bars for assigned companies.

## Market observations

Use Yahoo Finance's public, unofficial chart data for adjusted daily OHLCV. This source can be delayed and its interface can change. Store the source timestamp as the quote time and calculate freshness from that actual time; do not reset freshness to fetch time. Quotes older than 120 seconds cannot support a new fill. In particular, stale after-hours observations must not be presented as current executable prices.

Use up to 1,260 daily candles. Preserve trading-session gaps; do not synthesize weekend or holiday bars. A symbol with fewer than 300 usable observations is insufficient for statistical admission. The current exchange-local date is excluded conservatively. Missing historical adjustment metadata blocks statistical admission. Invalid and incomplete values are rejected or excluded. Weekends do not age a daily equity signal by trading sessions; holidays count conservatively as weekdays.

## Evidence and validation

Keep source URLs, observed and publication times, content hashes and source IDs through every handoff. Distinguish verified facts from interpretation and speculation. Deduplicate syndicated reporting by canonical identity and shared underlying event; repeated copies are not independent cross-checks. Freeze candidate rules before chronological holdout validation. Host code computes all statistics and stores rejections and insufficient-history outcomes.

Paper execution uses deterministic checks and simulated fills only. The executor has no broker credentials or order-submission capability. Pausing must prevent additional provider work and execution after the current bounded operation reaches its checkpoint.

## Startup, configuration and controls

Install the per-user macOS service with `python3 scripts/prismctl.py install`. The control token remains private and is linked to the console's Keychain entry. `PRISM_STATE_DIR` and `PRISM_CONTROL_TOKEN_FILE` select alternate state/token locations. The listener binds `127.0.0.1:8787`; `/v1/state`, `/v1/control` and `/v1/config` require the local bearer token and reject browser origins.

The existing OpenAI key is loaded from `PRISM_OPENAI_KEY_FILE`, `OPENAI_API_KEY`, the private `.prism-runtime/secrets/openai.key` file or the `studio.prism.openai` Keychain entry. Source URLs must never contain credentials. Model-call and spending ceilings are optional; null disables a ceiling while usage accounting remains active. Spend is a conservative local token estimate, not a provider invoice.

Use `python3 scripts/prismctl.py start`, `pause`, `resume`, `stop` or `run`. Run requests an immediate cycle and enables continued scheduling. Quotes refresh independently every 60 seconds while active; research repeats 15 minutes after a cycle completes. The Mac must remain awake and connected.

Save a partial configuration such as the following, then run `python3 scripts/prismctl.py configure --config-file /path/to/settings.json`:

```json
{
  "sector": {"max_symbols": 100, "discovery_enabled": true},
  "ingestion": {"shared_evidence_limit": 300, "max_content_chars": 32000}
}
```

Discovery tries the SEC company directory and falls back to Nasdaq's public listed-company directories when unavailable. ETF and test listings are excluded from exchange discovery. Source failures and unverified seed fallback remain visible. Upgrading archives prior crypto configuration, markets, approvals and the paper account atomically; the equity account starts with fresh USD paper cash, paused.

The fetcher reads RSS, Atom and ordinary HTML. It does not render JavaScript or extract PDFs. Unsupported documents retain any available feed summary and a fetch gap. Bing News RSS links may expose a summary or redirect page instead of a complete publisher article. Default feeds also include NVIDIA announcements, HPCwire, Semiconductor Engineering and Data Center Dynamics. Google News RSS is excluded because its robots policy blocked the search feed in the live check. Public source availability and coverage can change.
