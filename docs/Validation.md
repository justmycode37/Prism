# Validation records

This page preserves earlier validation and public-source observations. This documentation finalization used no credentials, installed service, external data requests, paid research cycles, or real exchange execution. The historical results below predate the latest continuous-runtime additions and are not the final verification status for the current branch.

## Prior automated checks recorded on October 30, 2026

- `PYTHONPATH=backend python3 -m unittest discover -s backend/tests -q`: 198 tests passed in that earlier run. This is a historical count, not the final current-branch suite result.
- `scripts/check-ui-contract.sh`: passed. The real backend state decodes into Swift models with 33 agents, four layers, equity observations, dynamic company provenance, nullable daily changes, optional legacy metadata, descending development returns, stable ties, missing returns, and test-period decoding.
- `xcodebuild -project Prism.xcodeproj -scheme Prism -configuration Debug -derivedDataPath /tmp/prism-ui-build CODE_SIGNING_ALLOWED=NO build`: succeeded. The existing App Intents metadata warning remains informational.
- `git diff --check`: passed.

Coverage includes dynamic listed-company admission, source provenance and ambiguous-name rejection; collection of an 80-symbol universe across 24 scouts; cross-collector sharing of article bodies longer than 2,200 characters; acquisition completion before model fan-out; call-budget deferral and checkpoint reuse; pause behavior; atomic legacy configuration/account migration; and retrieval of 2,300 evidence records.

Market fixtures cover actual Yahoo chart metadata, adjustments, missing daily changes, malformed quotes and symbols, duplicate sessions, incomplete bars, daily calendar gaps, 252-session Sharpe annualization, insufficient listing history, and weekend signal timing. Additional checks cover point-in-time availability, immutable experiments and dataset manifests, prospective review gates, shared portfolio replay, corporate actions, partial fills, economic signal deduplication, deterministic candidate search, development leaderboards, chart-storage reuse and lightweight quote refreshes. Historical results remain development evidence; promotion requires observations collected after registration.

## October 30 broad offline research run

The complete deterministic fixture grid registered and evaluated 1,320 unique candidates across momentum, mean reversion, breakout, and numeric event surprise. The dataset has 480 synthetic daily sessions, three candidate securities, and two benchmarks. All candidates have measured development returns; all were rejected by the full screen. None were promoted. This run tests software behavior, not market profitability.

The front-page projection returned the top 100 of 1,320 candidates with finite returns sorted descending, unique experiment identities, explicit test dates, and development-only qualification. A second identical search reused the complete cached report and produced exactly the same results. The state projection completed in 0.132 seconds on this machine; this is one fixture measurement, not a production latency guarantee.

Artifacts from the run are in `/tmp/prism-offline-20261030/`: `dataset.json`, `final-research.sqlite3`, `final-report.json`, `cached-report.json`, `state.json`, and `summary.json`. Reproduce from scratch with:

```sh
python3 scripts/make_research_fixture.py --output /tmp/prism-fixture.json
python3 scripts/research.py search --journal /tmp/prism-fixture.sqlite3 \
  --dataset /tmp/prism-fixture.json --max-candidates 1320 \
  --output /tmp/prism-fixture-report.json
```

That earlier run covered captured prospective proofs, rejected backdating and unattested observations, raw-price/corporate-action/benchmark fingerprints, replay equivalence, full-batch preregistration and nested alpha allocation, cache reuse and failed-search recovery, original source context and packet truncation, imported Monid receipts, credential isolation, real paper-ledger capture, and incomplete external costs blocking qualification.

Current branch regression files add spot conditional-stop protection, spot quote provenance, quote-only inter-signal accounting and expiry, durable research-memory reconciliation and risk/rejection reviews, and actual `ContinuousServices` worker-thread checks. In particular, `test_concurrent_runtime.py` covers stalled and budget-exhausted scheduler planning while market ingestion, protection, and reconciliation continue; worker joins on shutdown; and reuse of completed planning after restart. The final October 30 full offline suite passed 359 tests in 24.009 seconds; Python compilation, Swift contract validation, the unsigned Debug Xcode build, and `git diff --check` also passed. See [Offline system verification](SystemVerification.md) for scope and external activation checks.

An earlier Monid check used a private key for free authentication; it made no paid Monid run. News/transcript requests and receipt mapping were tested with injected offline responses; explicit command preview also made zero requests. This is prior work, not credential use during the current documentation finalization. See [Monid integration](Monid.md).

## Prior live public-source checks (October 29, 2026)

Read-only checks through the production fetcher reached the Nasdaq listing directory, Bing News RSS, NVIDIA announcements, HPCwire, Semiconductor Engineering and Data Center Dynamics. A focused Credo news query admitted CRDO outside the configured seeds with nine source records. SEC returned HTTP 403; the exchange-directory fallback supplied listings and the gap remained visible.

The public Yahoo endpoint returned MRVL equity metadata and 1,254 usable closed daily sessions. Quote time remained the source's market timestamp, and the five-year chart baseline was not substituted for a daily percentage change.

Google News RSS search was blocked by its robots policy and is excluded from defaults. Some company sites also returned HTTP 403. Default coverage combines sources, and retrieval failures remain observable.

## Limits

No paid model calls, real orders, service restart or graphical app interaction was used for these checks. Provider handoffs and strategy admission use offline fixtures in tests. Successful checks establish neither exhaustive coverage nor profitable strategies.

Yahoo is an unofficial source and may be delayed or revised. Daily histories exclude the current exchange-local date conservatively. Its historical execution-price basis is explicitly unverified, so the stricter replay protocol requires independently verified execution history and observed session boundaries. Weekends do not age an equity signal by trading sessions; holidays count conservatively as weekdays. Fresh opening observations remain necessary for paper fills. Short listing histories can remain insufficient for development evaluation.

The fetcher handles RSS, Atom and ordinary HTML, with finite depth, bytes, requests and time. It does not render JavaScript, parse PDFs, or bypass robots and access controls. Sector membership is an evidence-based classifier with operator-supplied seeds; it can miss companies and requires review when source context is ambiguous.
