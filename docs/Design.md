# Prism — macOS design plan

30 October 2026 · current implementation

## Final visual direction

The existing floating glass window and inset icon rail remain the visual foundation. Every page now uses a shared family of restrained frosted widgets: 22-point corners, 20-point content padding, 16-point page spacing, quiet outline symbols, and rounded numeric typography. Native clear glass remains on navigation and controls; translucent material keeps data widgets readable over the window backdrop.

Prism opens with two matching glass choices: Single SOL Trader and Agent Swarm. The swarm retains the five-section rail. The single trader opens a separate, rail-free workspace centered on SOLUSDT, followed by account metrics, an explicitly labeled paper ledger, and the trader's latest output. The grid control returns to the chooser. Both workspaces share appearance settings. Opening a workspace changes navigation; selecting its runtime mode is a separate explicit action, available while paused and subject to the existing flat-position checks.

The SOL chart displays up to 120 actual one-minute SOLUSDT linear OHLC candles from the runtime market-data service or its persisted history. Monochrome frosted bodies use filled and hollow treatment to distinguish direction, with thin wicks on an inset glass surface. The wider price and time viewport keeps price action and individual candles compact while the chart frame remains the same size; the current forming candle stays visually lighter. Venue environments remain isolated, and the chart stays empty until real candle history is available. Account trading P&L and API costs stay separate. Pending SOL execution eligibility is shown independently from mode selection. Start can enable observation while eligibility is pending; backend execution approvals remain authoritative, and Pause stays visible in the header.

Results, agent roles, performance and estimated running costs take priority. Details such as role manuals, source versions, operational journals, and failure reasons stay reachable through disclosure controls. There is no decorative animation or saturated dashboard background.

## Dimensions and behavior

| Element | Current specification |
|---|---|
| Main window | 840 × 560-point initial content size; minimum 600 × 420; no custom maximum |
| Resize | Normal AppKit resizable window with hidden titlebar/traffic-light visuals; drag edges or corners |
| Position | Move by dragging empty glass; remember main window frame |
| Corners | 38-point glass radius |
| Navigation | Compact icon-only rail, native clear glass, tooltips and accessibility labels |
| Overview | All-time paper P&L and percentage, estimated model spend today with request count, and agent activity. One Start/Stop control, followed by the top five development results; expand rows for metrics and qualification, or show all loaded results. Test-period filtering is preserved. |
| Sections | Overview, Research, Test, Execution, Agents — five rail items |
| Pages | Research shows hypotheses, evidence and sources. Test separates prospective reports from historical results. Execution shows operational performance and component costs, followed by the paper ledger and searchable activity. Agents presents each reported role with staffing, mandate and latest output; individual assignments, hierarchy and manuals are expandable. |
| Settings | Spend today, requests and ceiling, appearance controls, independent blur/milkiness sliders, and explicit-save daily spending controls |

The main window uses native window resizing with visually hidden chrome. Settings opens from the small upper-right settings control within the same glass window, outside the five-section rail. The user can close the main window and reopen it from the Dock or Window → Show Prism.

## Rendering architecture

The `installGlass` method in `PrismApp.swift` retains the approved GitHub renderer. A clear AppKit container holds two separate layers: the native glass backdrop, then the transparent SwiftUI hosting view. Keeping these as siblings prevents the backdrop from sampling controls. Operational pages add translucent material widgets over the same surface. Content widgets do not use optical liquid glass, so their text stays separate from the native control sampling layer. The navigation rail uses clear glass rather than a milky regular-glass plate.

The prototype vendors [WholeWindowLiquidGlass](https://github.com/vientooscuro/WholeWindowLiquidGlass), which uses `NSGlassEffectView` and adjusts its private rendering filters. The progressive blur bands use equal nonzero weights on the main window backdrop, so the radius controls visible blur across its surface. Smaller controls retain their existing glass treatment. A Background blur slider controls the backdrop radius from 0 to 100, defaulting to 40, shared by Light and Dark. It updates the existing renderer immediately without changing tint, opacity, brightness, or saturation settings. A separate Milkiness slider controls the appearance veil from 0% to 100%, defaulting to 0%: no added white overlay in Light or black overlay in Dark. Both values persist locally. Milkiness is a compositing opacity, not an exact measured transmission percentage.

`GlassSurface` adds only diffuse grazing light and the appearance veil. A crisp border is added solely when Increase Contrast is enabled. Native glass handles refraction; no screenshot, wallpaper copy, or screen-recording permission is used.

The local build enables `PRISM_EXPERIMENTAL_GLASS`. Removing that compilation condition excludes the private implementation and uses the public [NSGlassEffectView](https://developer.apple.com/documentation/appkit/nsglasseffectview) fallback. The sharper prototype renderer requires OS-version testing and cannot be included in an App Store target. See `ThirdParty.md` for source provenance and licenses.

## Appearance and accessibility

System is the default. Light and Dark overrides, blur, and milkiness persist in the app's own preferences. Settings uses the same material as the rest of the app, with a stronger glass treatment on the selected appearance control.

Reduce Transparency replaces the decorative surface with an opaque equivalent. Increase Contrast supplies a stronger boundary. The experimental tuner stops applying changes when either accommodation is enabled, allowing native material updates to take effect. The blur slider is disabled while these accommodations override it; milkiness is disabled with Reduce Transparency. The public fallback build omits the blur slider because it does not expose a backdrop radius. There is no animated decorative glow; the renderer does maintain a display link to reapply its glass adjustments as AppKit rebuilds its layers. Profile that idle work before production distribution.

The Start/Stop control exposes an accessible label for starting or stopping all agents. Navigation icons have page labels and tooltips. Appearance buttons expose their selection state. Command-comma opens settings; Command-W closes the key window; Command-0 shows Prism; Command-Q quits. The standard Edit menu supports text editing and secure-field paste.

## Source layout

- `App/PrismApp.swift`: application lifecycle, native window behavior, shared glass installation, and menus.
- `Design/GlassSurface.swift`: diffuse finish, frosting, accessibility appearance.
- `Design/PrismWordmark.swift` and `PrismTypography.swift`: retained wordmark assets, no longer rendered in the current interface.
- `Views/PrismHomeView.swift`: glass rail and shared page container.
- `Views/PrismOverviewView.swift`: P&L, spending, agents, development leaderboard and runtime controls.
- `Views/ConsoleComponents.swift`: shared frosted cards, responsive metric grids, outline icon badges, status and empty states.
- `Views/PrismSettingsView.swift`: appearance and explicit-save spending controls.
- `Appearance/AppearancePreferences.swift`: stored appearance, blur, and milkiness preferences.
- `Vendor/WholeWindowLiquidGlass.swift`: attributed renderer behind a compilation condition.
- `Resources/Fonts/Raleway.ttf`: retained font asset; licenses included in app resources.

Xcode previews show the SwiftUI composition. Judge desktop refraction in the running app, because a preview or an isolated window capture does not reproduce the complete on-screen backdrop.

## Operational boundaries

Keep the workspace chooser as the launch state, with Overview as the swarm's resting state. Cost labels distinguish today’s estimated model spend, reported research totals, and operation component budgets. Missing figures remain unavailable; costs are not allocated to individual agents without reported data. Historical returns are development evidence and remain separate from prospective qualification.

Operational pages show actual backend state, failed experiments, role outputs, market observations, and ledger-derived simulated P&L. Detailed findings stay behind disclosure controls. Offline state is explicit; previous snapshots are identified as stale.

Before live trading, provide visible environment, runtime health, data freshness, incidents, and emergency controls. Explain the difference between pausing research, blocking new exposure, canceling entries, and flattening positions. The current Overview is not the full future trading control surface.

## Remaining review

Validate window dragging/resizing on the actual Mac, keyboard focus, VoiceOver, contrast over varied backdrops, system accessibility accommodations, and idle energy use. Current build and interaction observations are recorded in `Validation.md`.

## Offline layout checks

`scripts/check-ui-rendering.sh` builds a separate fixture renderer with an inert runtime client. It excludes the production app and client, so it cannot start the installed service, read control credentials, or call a provider. It renders the chooser, both workspaces and six swarm pages at 840 × 560 and 600 × 420 in both appearances, plus offline, single-candle, mode-mismatch and eligibility-pending states. Outputs go to `/tmp/prism-ui-redesign/`. `scripts/check-workspace-contract.sh` verifies workspace matching and bounded OHLC candle validation entirely offline. These images check content layout; desktop refraction still requires an explicitly authorized running-app check.
