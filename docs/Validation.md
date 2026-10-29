# Validation

This record covers the reverse-cascade rebuild on 29 October 2026. Historical validation of the removed backend is not evidence for this implementation.

## Checks

- **Passed:** `scripts/check-ui-contract.sh` against state assembled by the new runtime.
- **Passed:** Python syntax compilation of the new launcher and CLI; CLI argument/help smoke check.
- **Passed:** XcodeGen project regeneration, removing stale role-manual references.
- **Passed:** Xcode Debug build with Swift 6 and code signing disabled. Artifact: `build/Cascade/Build/Products/Debug/Prism.app`; build log: `/tmp/prism-cascade-xcodebuild.log`. CoreSimulator service diagnostics and skipped App Intents metadata extraction did not prevent the macOS build.
- **Passed:** 57 backend tests, including the complete synthetic four-layer cascade through real statistical validation and paper execution, budget/restart checkpoints, stop handling, provenance rejection, statistical lookahead defenses, costs, exposure, quote freshness, collector HTTP behavior, strict model schemas and the authenticated loopback API. The loopback checks require local networking permission in a restricted environment.
- **Passed:** SHA-256 comparison of all 22 files under `Prism/` against the pre-rebuild snapshot: every file is unchanged.
- **Passed:** read-only public collection smoke tests retrieved news evidence from the five default publishers and 999 closed BTC spot candles. Blocked feeds were removed from defaults; unsupported ECB PDF links retain the feed summary.
- **Passed:** installed service reports `reverse_cascade`, layer counts 24/6/2/1, paper mode, paused status, credential configured, zero calls and zero spend.

The fixture uses a temporary database and injects an empty credential loader, so decoding checks make no provider calls and do not touch the user's live database.

The UI contract check generates state with the real new runtime, compiles the unchanged `Prism/Models/RuntimeState.swift`, and checks the 33-agent roster, four layers, model routing, the existing Overview Start action, nullable strategy metrics, market fractions, paper records, P&L and Settings JSON encoding.

## Scope

No paid OpenAI calls or live exchange orders were made. The existing macOS login service was replaced with the new backend after checks passed, reusing its existing control-token link; the new service starts paused. No claim of a profitable strategy, complete web coverage or live model compatibility follows from mocked tests. The new database starts paused.

The SwiftUI presentation remains unchanged. Full VoiceOver, keyboard navigation, minimum window sizing, appearance regression and desktop-dependent glass rendering remain manual checks. The private glass prototype continues to require OS-update regression testing.
