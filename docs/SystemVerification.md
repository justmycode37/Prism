# Offline system verification

`backend/tests/test_system_e2e.py` verifies the registered research-to-paper-execution path with a fake clock and synthetic Bybit messages. The synthetic price path is only a deterministic test fixture; it is not a financial result or performance claim.

Run both focused files from `backend/`:

```sh
python3 -m unittest discover -s tests -p 'test_system_e2e.py' -v
python3 -m unittest discover -s tests -p 'test_concurrent_runtime.py' -v
```

## Covered flow

The integration test uses the real `Store`, `MarketDataService`, `LiveResearch`, `ResearchMemory`, `ResearchWorker`, `EventBus`, `ResponsesAgent`, `BybitAdapter`, and `PaperExchange`. The public REST history and Responses calls are injected offline fixtures. This focused research-to-execution test does not start `ContinuousServices`, the installed service, or a scheduler loop.

The test repairs a missing REST history candle, verifies that recovered REST bars remain historical, evaluates 300 pre-registration closed bars through the actual development evaluator, and then feeds fresh WebSocket candle closures after registration. Reviews are created from a durable market event and processed without a source collector; a later review request receives the prior conclusion from the persisted dossier.

The prospective path creates 60 consecutive UTC-day marks from actual immutable MarketData snapshots. Twelve synthetic completed trades produce twelve positive five-day blocks against flat same-instrument and cash baselines. Assertions retain the registered fee, slippage, funding, operating-cost, benchmark, multiple-comparison correction, and block-correlation checks. Eligibility remains false until the exact strategy version and allocation are explicitly configured.

The execution portion routes injected GPT-6.1 Sol function calls through the real host tools. A pre-60-day request, unknown snapshot, and stale snapshot are blocked. After qualification and operator approval, a valid order is durably acknowledged with zero filled quantity; a later quote advances the local paper exchange, produces an execution, updates ResearchMemory, and remains deduplicated when the controller is reconstructed.

## Spot quote boundary

The second test covers the real spot ticker, level-one orderbook, and kline helpers and asserts MarketData's composite quote provenance (`orderbook+ticker`). Integration found and fixed a mismatch in research quote validation: the spot composite now retains both component timestamps and is accepted only when their minimum equals the quote timestamp. `test_live_research` additionally feeds that real MarketData snapshot into prospective accounting and rejects it after expiry. The full 60-day qualification flow uses the linear ticker path; no test rewrites the stored snapshot or bypasses the evidence gate.

## ContinuousServices worker isolation

`backend/tests/test_concurrent_runtime.py` runs the actual `ContinuousServices.run()` worker topology against local REST/WebSocket fixtures and an injected model transport. It confirms that a blocked scheduler request and an exhausted scheduler budget do not stop public ticker ingestion or the independent protection and reconciliation workers. It also verifies shutdown joins the worker threads and restart reuses a completed scheduler plan without another model request. The fixture's credential loader raises if called; no installed service, credential, exchange, or provider is accessed.

## Final local verification (October 30, 2026)

`PYTHONPATH=backend python3 -m unittest discover -s backend/tests -q` passed
all 359 tests in 24.009 seconds. The full run includes base-currency Spot fees,
explicit sub-step protection dust, and mixed-currency execution fees across
restart. Python compilation, `git diff --check`, and the Swift runtime contract
check passed. The unsigned Debug Xcode build also succeeded.

All runtime integration checks used temporary journals, fake clocks, synthetic
market/calendar data, and injected exchange/model transports. No installed
service, exchange or model credentials, paid research, or real orders were
used. Authenticated Bybit behavior, live calendar vendor completeness, and
actual provider availability remain external activation checks.

## Sol routing follow-up

The GPT-6.1 Sol follow-up passed all 361 offline tests in 25.714 seconds,
including saved-configuration migration, historical charge preservation,
legacy advisory task routing, and the operational tool loop. The migration
checks also passed after adding a write transaction around the upgrade.
The Swift contract check and unsigned Debug Xcode build passed. No installed
service was started or reconfigured during verification.
