# Validation

Automated checks verified on October 30, 2026. Public-source observations below were recorded on October 29.

## Automated checks

- `PYTHONPATH=backend python3 -m unittest discover -s backend/tests -q`: 143 tests passed.
- `scripts/check-ui-contract.sh`: passed. The real backend state decodes into Swift models with 33 agents, four layers, equity observations, dynamic company provenance, nullable daily changes and optional legacy metadata.
- `xcodebuild -project Prism.xcodeproj -scheme Prism -configuration Debug -derivedDataPath build CODE_SIGNING_ALLOWED=NO build`: succeeded. The existing App Intents metadata warning remains informational.
- `git diff --check`: passed.

Coverage includes dynamic listed-company admission, source provenance and ambiguous-name rejection; collection of an 80-symbol universe across 24 scouts; cross-collector sharing of article bodies longer than 2,200 characters; acquisition completion before model fan-out; call-budget deferral and checkpoint reuse; pause behavior; atomic legacy configuration/account migration; and retrieval of 2,300 evidence records.

Market fixtures cover actual Yahoo chart metadata, adjustments, missing daily changes, malformed quotes and symbols, duplicate sessions, incomplete bars, daily calendar gaps, 252-session Sharpe annualization, insufficient listing history, and weekend signal timing. Additional checks cover point-in-time availability, immutable experiments and dataset manifests, prospective review gates, shared portfolio replay, corporate actions, partial fills, economic signal deduplication, deterministic candidate search, development leaderboards, chart-storage reuse and lightweight quote refreshes. Historical results remain development evidence; promotion requires observations collected after registration.

## Live public-source checks

Read-only checks through the production fetcher reached the Nasdaq listing directory, Bing News RSS, NVIDIA announcements, HPCwire, Semiconductor Engineering and Data Center Dynamics. A focused Credo news query admitted CRDO outside the configured seeds with nine source records. SEC returned HTTP 403; the exchange-directory fallback supplied listings and the gap remained visible.

The public Yahoo endpoint returned MRVL equity metadata and 1,254 usable closed daily sessions. Quote time remained the source's market timestamp, and the five-year chart baseline was not substituted for a daily percentage change.

Google News RSS search was blocked by its robots policy and is excluded from defaults. Some company sites also returned HTTP 403. Default coverage combines sources, and retrieval failures remain observable.

## Limits

No paid model calls, real orders, service restart or graphical app interaction was used for these checks. Provider handoffs and strategy admission use offline fixtures in tests. Successful checks establish neither exhaustive coverage nor profitable strategies.

Yahoo is an unofficial source and may be delayed or revised. Daily histories exclude the current exchange-local date conservatively. Its historical execution-price basis is explicitly unverified, so the stricter replay protocol requires independently verified execution history and observed session boundaries. Weekends do not age an equity signal by trading sessions; holidays count conservatively as weekdays. Fresh opening observations remain necessary for paper fills. Short listing histories can remain insufficient for development evaluation.

The fetcher handles RSS, Atom and ordinary HTML, with finite depth, bytes, requests and time. It does not render JavaScript, parse PDFs, or bypass robots and access controls. Sector membership is an evidence-based classifier with operator-supplied seeds; it can miss companies and requires review when source context is ambiguous.
